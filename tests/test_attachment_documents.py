"""Attached PDFs are parsed in a bounded worker; a hostile one is refused, never the app's problem."""
from contextlib import nullcontext
import io
import time
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

from blackrose.attachments import documents
from blackrose.attachments.documents import MAX_DOCUMENT_PAGES, InvalidDocument, inspect_document
from blackrose.attachments.pdf_worker import CPU_SECONDS, MEMORY_LIMIT
from blackrose.documents import render_pdf


def blank_pdf(pages=1, password=None):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def worker(code):
    """A worker command that runs the real worker with ``parse_pdf`` replaced by ``code``."""
    return ('-I', '-c', 'import resource, sys\nfrom blackrose.attachments import documents, pdf_worker\n'
            f'documents.parse_pdf = lambda data: {code}\nsys.exit(pdf_worker.main())')


class PdfAttachmentTests(unittest.TestCase):
    def refused(self, data=b'%PDF-1.4\n', **patches):
        with patch.multiple(documents, **patches) if patches else nullcontext(), \
                self.assertRaises(InvalidDocument) as caught:
            inspect_document(data, claimed_name='report.pdf')
        return str(caught.exception)

    def test_a_readable_pdf_is_read_in_the_worker(self):
        text, info = inspect_document(render_pdf('# Quarterly\n\nRevenue grew.'), claimed_name='q.pdf')
        self.assertEqual(text, 'Quarterly\nRevenue grew.')
        self.assertEqual((info.pages, info.mime_type), (1, documents.PDF_TYPE))

    def test_refusals_keep_their_reasons(self):
        self.assertEqual(self.refused(blank_pdf(password='secret')), 'the PDF is encrypted')
        self.assertEqual(self.refused(blank_pdf(MAX_DOCUMENT_PAGES + 1)),
                         f'the PDF has {MAX_DOCUMENT_PAGES + 1} pages and the limit is {MAX_DOCUMENT_PAGES}')
        self.assertTrue(self.refused(b'%PDF-1.4 truncated').startswith('the PDF could not be read'))
        self.assertEqual(self.refused(blank_pdf()), 'the document contains no readable text')

    def test_the_worker_runs_under_cpu_and_memory_limits(self):
        command = worker('(str((resource.getrlimit(resource.RLIMIT_AS)[0], '
                         'resource.getrlimit(resource.RLIMIT_CPU)[0])), 1)')
        with patch.object(documents, '_PDF_WORKER', command):
            text, _ = inspect_document(b'%PDF-1.4\n', claimed_name='a.pdf')
        self.assertEqual(text, str((MEMORY_LIMIT, CPU_SECONDS)))

    def test_a_pdf_that_never_finishes_is_refused_within_the_bound(self):
        started = time.monotonic()
        message = self.refused(_PDF_WORKER=('-c', 'import time; time.sleep(60)'), PDF_TIMEOUT_SECONDS=1)
        self.assertEqual(message, 'the PDF took too long to read')
        self.assertLess(time.monotonic() - started, 10)

    def test_a_pdf_that_exhausts_memory_is_refused(self):
        started = time.monotonic()
        message = self.refused(_PDF_WORKER=worker('bytearray(4 * 1024 ** 3)'))
        self.assertEqual(message, 'the PDF needs more memory to read than is allowed')
        self.assertLess(time.monotonic() - started, 10)

    def test_a_worker_killed_by_its_cpu_limit_or_failing_leaks_nothing(self):
        for code in ("import os, signal; os.kill(os.getpid(), signal.SIGXCPU)",
                     "print('private page text'); raise SystemExit('Traceback: private page text')"):
            with self.subTest(code=code):
                message = self.refused(_PDF_WORKER=('-c', code))
                self.assertEqual(message, 'the PDF needs more time or memory to read than is allowed')


if __name__ == '__main__':
    unittest.main()
