"""The embedder measurement refuses incomplete caches before model loading."""
import tempfile
import unittest
from pathlib import Path

from local_knowledge.config import Config
from scripts.measure_retriever_embedder import DeterministicControl, check_snapshot


class EmbedderMeasurementTests(unittest.TestCase):
    def test_snapshot_preflight_fails_closed_without_creating_cache_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config(cache_dir=temporary)
            model = Path(temporary) / 'models--google--embeddinggemma-2'
            snapshot = model / 'snapshots' / config.model_revision
            snapshot.mkdir(parents=True)
            with self.assertRaisesRegex(FileNotFoundError, 'No download was attempted') as error:
                check_snapshot(config)
            self.assertIn('model.safetensors', str(error.exception))
            self.assertEqual(list(snapshot.iterdir()), [])

    def test_fake_control_identity_and_vectors_match_config_dimensions(self):
        provider = DeterministicControl(768)
        vector = provider.embed_query('database forests')
        self.assertEqual(provider.identity(), {
            'measurement_control': 'deterministic-v1', 'dimensions': 768})
        self.assertEqual(len(vector), 768)
        self.assertAlmostEqual(sum(value * value for value in vector), 1.0)

