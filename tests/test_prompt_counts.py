"""Shared prompt estimates preserve admission and image identity semantics."""
import base64
import json
import math
import unittest
from unittest.mock import patch

from blackrose import prompts
from blackrose.telemetry import GenerationMetrics


def image(width):
    data = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + width.to_bytes(4, 'big') + (28).to_bytes(4, 'big')
    return {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(data).decode()}}


class PromptCountsTests(unittest.TestCase):
    def test_shared_counts_keep_both_arithmetic_and_one_serialization(self):
        messages = [{'role': 'user', 'content': ['é', image(28)]}]
        tools = [{'name': 'tool'}]
        with patch.object(prompts, 'request_messages', side_effect=lambda m: m), patch.object(prompts.json, 'dumps', wraps=json.dumps) as dumps:
            counts = prompts.count_prompt(messages, tools)
            metrics = GenerationMetrics({}, messages, tools, lambda _: None, counts=counts)
            prompt = prompts.Prompt(messages, '', counts=counts)
            builder = prompts.PromptBuilder(None, None, None, {})
            builder.admit(prompt, tools)
            self.assertEqual(dumps.call_count, 1)
        self.assertEqual(metrics.admission_input_bound, counts.text_bytes + counts.framing + counts.images)
        self.assertEqual(metrics.prompt, math.ceil(counts.text_bytes / 4) + counts.framing + counts.images)
        self.assertIsNone(prompt.counts)

    def test_image_cache_uses_content_and_is_bounded(self):
        prompts._image_counts.clear()
        with patch.object(prompts, 'inspect_image', wraps=prompts.inspect_image) as inspect:
            for width in (28, 28, 42):
                prompts.count_prompt([{'role': 'tool', 'content': [image(width)]}], [])
            self.assertEqual(inspect.call_count, 2)
        self.assertNotEqual(prompts._image_tokens(image(28)['image_url']['url']),
                            prompts._image_tokens(image(42)['image_url']['url']))
        with patch.object(prompts, '_IMAGE_CACHE_LIMIT', 2):
            prompts._image_tokens(image(56)['image_url']['url'])
            self.assertLessEqual(len(prompts._image_counts), 2)

    def test_cloud_context_and_separate_input_limit_are_enforced(self):
        model = {'provider': 'opencode-go', 'context_length': 1000000,
                 'limit': {'context': 1000000, 'input': 300000}}
        builder = prompts.PromptBuilder(None, None, None, model)
        builder.admit(prompts.Prompt([{'role': 'user', 'content': 'a' * 150000}], ''), [])
        with self.assertRaisesRegex(prompts.InputRejected, 'Provider input limit: 300,000'):
            builder.admit(prompts.Prompt([{'role': 'user', 'content': 'a' * 300000}], ''), [])
        model['context_length'] = 0
        with self.assertRaisesRegex(prompts.InputRejected, 'invalid context capacity'):
            prompts.context_budget(model)


if __name__ == '__main__':
    unittest.main()
