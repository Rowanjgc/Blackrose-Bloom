"""Render Markdown into real PDF bytes without HTML, network or subprocesses."""
from html import escape
import io

from markdown_it import MarkdownIt


def render_pdf(markdown):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    # Use the renderer's bundled Vera font for readable Latin text.
    from reportlab import rl_config
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from pathlib import Path
    font = next((Path(p) / 'Vera.ttf' for p in rl_config.TTFSearchPath
                 if (Path(p) / 'Vera.ttf').is_file()), None)
    if font:
        if 'BlackroseVera' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('BlackroseVera', str(font)))
        for style in styles.byName.values():
            style.fontName = 'BlackroseVera'
    tokens = MarkdownIt('commonmark').enable('table').parse(markdown)
    story = []
    heading = None
    list_depth = 0
    table_rows = None
    row = None
    for token in tokens:
        if token.type == 'heading_open':
            heading = f'Heading{min(int(token.tag[1:]), 3)}'
        elif token.type in ('bullet_list_open', 'ordered_list_open'):
            list_depth += 1
        elif token.type in ('bullet_list_close', 'ordered_list_close'):
            list_depth -= 1
        elif token.type == 'table_open':
            table_rows = []
        elif token.type == 'tr_open':
            row = []
        elif token.type == 'tr_close':
            table_rows.append(row)
        elif token.type == 'table_close':
            if table_rows:
                count = max(len(r) for r in table_rows)
                if count > 12:
                    raise ValueError('PDF tables support at most 12 columns')
                table = Table(table_rows, colWidths=[468 / count] * count, repeatRows=1, hAlign='LEFT')
                table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eee8eb')),
                                          ('GRID', (0, 0), (-1, -1), .3, colors.grey),
                                          ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
                story.extend([table, Spacer(1, 10)])
            table_rows = None
        elif token.type == 'inline':
            # Treat every model string as text. No reportlab XML, URLs, images,
            # font names or external resources are accepted as renderer commands.
            text = escape(token.content).replace('\n', '<br/>')
            paragraph = Paragraph(('• ' if list_depth else '') + text, styles[heading or 'BodyText'])
            if table_rows is not None:
                row.append(paragraph)
            else:
                story.append(paragraph)
                story.append(Spacer(1, 5))
            heading = None
        elif token.type in ('fence', 'code_block'):
            story.extend(Paragraph(escape(line) or ' ', styles['Code'])
                         for line in token.content.splitlines())
    output = io.BytesIO()
    SimpleDocTemplate(output, title='Blackrose document', leftMargin=72, rightMargin=72).build(
        story or [Paragraph(' ', styles['BodyText'])])
    return output.getvalue()
