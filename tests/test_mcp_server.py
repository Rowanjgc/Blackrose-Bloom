"""The locked MCP SDK still exposes the read-only Retriever tool contract."""
import json
from pathlib import Path
import tempfile
import unittest

from mcp.client import Client

from local_knowledge.config import Config
from local_knowledge.ingestion import import_file
from local_knowledge.mcp_server import create_server
from test_retriever import FakeProvider


class MCPCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_search_and_citation_open_through_the_sdk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(data_dir=str(root), database_path=str(root / 'knowledge.db'),
                            cache_dir=str(root / 'cache'), dimensions=128, chunk_tokens=32, chunk_overlap=4)
            source = root / 'reference.md'
            source.write_text('# Database\n\nDatabase transactions preserve records.\n')
            provider = FakeProvider()
            import_file(config, provider, source)
            async with Client(create_server(config, provider)) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual({tool.name for tool in tools},
                                 {'knowledge_search', 'knowledge_open', 'knowledge_list'})
                self.assertTrue(all(tool.annotations.read_only_hint and not tool.annotations.destructive_hint
                                    for tool in tools))
                found = await client.call_tool('knowledge_search', {'query': 'database transactions'})
                self.assertFalse(found.is_error)
                payload = json.loads(found.content[0].text)
                hit = payload['results'][0]
                opened = await client.call_tool('knowledge_open', {
                    'document_id': hit['document_id'], 'chunk_id': hit['chunk_id'], 'limit': 1})
                self.assertFalse(opened.is_error)
                self.assertIn('Database transactions preserve records.', opened.content[0].text)
                listed = await client.call_tool('knowledge_list', {'limit': 1})
                self.assertFalse(listed.is_error)
                self.assertIn(hit['document_id'], listed.content[0].text)
