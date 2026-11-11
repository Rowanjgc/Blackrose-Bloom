"""Per-turn tool validation, effects, evidence and artifact failure policy."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING
from collections.abc import Callable

if TYPE_CHECKING:
    from .citations import CitationAudit

from jsonschema import ValidationError

from .animations import normalize_arguments, repair_details
from .errors import RecoveryRequired
from .illustrations import CONTRACT, SKILL_ID, IllustrationDrafts, plan_markdown, prepare_illustration
from .tool_host import ToolHost
from .tools import MAX_TOOL_RESULT_CHARS, validate_call
from .library import Capture
from .outbound import OutboundDenied
from .prompts import ANIMATION_SKILL_ID

log = logging.getLogger(__name__)
MAX_IMAGE_JOBS = 4

# Every tool that can reach ToolExecutor.execute is in exactly one of these sets.
# A contract test keeps them aligned with the schemas advertised in tools.py.
HANDLERS = {
    'run_skill_script': '_run_skill_script',
    'activate_tools': '_activate_tools',
    'knowledge_add': '_knowledge_add',
    'illustration_draft': '_illustration_draft',
    'animation_plan': '_animation_plan',
    'animation_create_planned': '_animation_create_planned',
    'animation_create': '_animation_create',
    'image_gen': '_image_gen',
}
FILE_TOOL_NAMES = frozenset({'file_list', 'file_search', 'file_info', 'file_read',
                             'directory_create', 'document_create', 'file_edit'})
CONTROLLER_TOOLS = frozenset({'animation_read', 'knowledge_search', 'knowledge_open', 'knowledge_list',
                              'read_skill', 'web_search', 'fetch_content'})
ANIMATION_ATTEMPT_TOOLS = frozenset({'animation_create', 'animation_plan', 'animation_create_planned'})

# Per-tool failure policy: each hook shapes the failed result and updates turn state.
FAILURE_HOOKS = {
    'animation_plan': '_fail_motion_plan',
    'animation_create_planned': '_fail_motion_plan',
    'web_search': '_fail_destination_request',
    'fetch_content': '_fail_destination_request',
    'document_create': '_fail_document',
    'animation_create': '_fail_animation_create',
    'image_gen': '_fail_image_gen',
}


@dataclass
class ToolInvocation:
    """One call's state, shared between the dispatcher, its handler and the failure policy."""
    name: str
    arguments: Any
    image_started: bool = False
    generated: Any = None


@dataclass
class EffectBudget:
    """The host enforces effects even when the model repeats rejected calls."""
    image_jobs: int = 0
    image_error: str | None = None

    @property
    def images_available(self) -> bool:
        return self.image_error is None and self.image_jobs < MAX_IMAGE_JOBS

    def start_image(self) -> None:
        if not self.images_available:
            raise ValueError('Image execution is unavailable for the rest of this reply; '
                             'ask again in a new message.')
        self.image_jobs += 1


class ToolExecutor:
    def __init__(self, controller: Any, assistant: Any, prompt: Any, web: bool | None,
                 *, on_delta: Callable, on_tool: Callable | None = None,
                 on_images: Callable | None = None, on_animations: Callable | None = None,
                 animation_repair_id: int | None = None):
        # Handlers reach the controller only through this narrow surface.
        self.host = ToolHost(controller)
        self.assistant = assistant
        self.prompt = prompt
        self.web = web
        self.on_delta = on_delta
        self.on_tool = on_tool
        self.on_images = on_images
        self.on_animations = on_animations
        self.animation_repair_id = animation_repair_id
        self.effects = EffectBudget()
        self.drafts = IllustrationDrafts(self.host.illustration_drafts_root / str(assistant.conversation_id) / str(assistant.id))
        self.draft_attempted = False
        self.draft_valid = False
        self.illustration_requested = False
        self.illustration_created = False
        self.sources: list[dict] = []
        self.animation_attempted = animation_repair_id is not None
        self.animation_created = False
        self.document_attempted = False
        self.document_created = False
        self.document_error = None
        self.animation_error: str | None = None
        self.motion_plan = None
        self.motion_plan_started: float | None = None
        self.motion_plan_disabled = False
        self.rejected_motion_plan = None
        self.motion_plan_error: str | None = None
        self.activation_rounds = 0
        self.script_attempts = 0
        self.web_captures: dict[str, Capture] = {}
        self.citation_audit: CitationAudit | None = None
        # (request, builder, request options, stream budget), set by the turn once known.
        self.visual_review_context: tuple[Any, Any, dict, Any] | None = None

    def visual_review_callback(self, name):
        context = self.visual_review_context
        if (context is None or self.animation_repair_id is not None
                or not self.host.visual_review_available()):
            return None
        request, builder, options, budget = context
        async def review(scene):
            reviewed, result = await self.host.review_animation(scene, request, self.motion_plan,
                builder, options, budget, lambda phase: self.emit(name, phase))
            log.info('Animation visual review for message %s: %s, timings=%s',
                     self.assistant.id, result['assessment'], result.get('timings', {}))
            return reviewed, result
        return review

    def report_visual_review(self, result):
        review = result.get('visual_review')
        if not review or review['assessment'] == 'pass':
            return
        # Publish the status directly, rather than relying on the author model to
        # repeat a tool result. Findings remain untrusted model output.
        message = '\n\nVisual review did not establish a pass'
        if review.get('findings'):
            message += ': ' + '; '.join(
                f"{f['object']} at {f['timestamp']:.2f}s: {f['observation']}"
                for f in review['findings'][:3])
        else:
            message += ' (review unavailable or inconclusive)'
        self.on_delta(message + '.\n\n')

    def report_motion_check(self, result):
        motion = result.get('motion')
        if not motion or motion.get('reviewed') or not motion.get('notice'):
            return
        # Deterministic host finding; publish it directly so the author model
        # cannot silently omit a static-scene or element warning from its answer.
        self.on_delta('\n\n' + motion['notice'] + '\n\n')

    def definitions(self) -> list[dict]:
        if self.animation_repair_id is not None:
            from .tools import animation_tools
            return ([] if self.animation_created or not self.host.automatic_animation_repair
                    else animation_tools([self.animation_repair_id]))
        definitions = self.host.tool_definitions(
            self.web, xiaohei=self.prompt.xiaohei, animation_ids=self.prompt.animation_ids,
            animations_enabled=self.prompt.animations_enabled, skills_available=self.prompt.skills_available,
            library_write_enabled=self.prompt.library_write_enabled, ui_used=self.prompt.ui_used)
        if (self.prompt.xiaohei and self.prompt.plan_only) or not self.effects.images_available:
            definitions = [t for t in definitions if t['function']['name'] != 'image_gen']
        return definitions

    def emit(self, name: str, phase: str, result: dict | None = None) -> None:
        if self.on_tool:
            self.on_tool(name, phase, result)

    def require_successful_artifacts(self) -> None:
        if self.animation_attempted and not self.animation_created:
            raise RuntimeError('Animation creation failed; no animation was saved. '
                               + (self.animation_error or 'The model did not complete a valid animation.'))
        if self.document_attempted and not self.document_created:
            raise RuntimeError('Document creation failed; no output was saved. ' + (self.document_error or ''))
        if self.illustration_requested and not self.illustration_created:
            raise RuntimeError('Illustration generation did not complete; no image was generated.')
        if self.draft_attempted and not self.draft_valid:
            # A later invalid repair can follow an image that is already saved; do not
            # claim that nothing was generated when the artifact exists.
            detail = ('the saved illustration predates the rejected draft revision.'
                      if self.illustration_created else 'no image was generated.')
            raise RuntimeError('Illustration draft has unresolved blocking issues; ' + detail)
        if self.effects.image_error:
            raise RuntimeError('Image generation failed: ' + self.effects.image_error)

    async def preauthorize(self, calls: list[dict]) -> None:
        """Ask once about this round's web requests that need approval, before any call runs."""
        if not self.host.outbound_active:
            return
        requests = []
        for call in calls:
            name = call['function']['name']
            if name not in ('web_search', 'fetch_content'):
                continue
            try:
                arguments = json.loads(call['function']['arguments'])
                validate_call(self.definitions(), name, arguments)
            except (ValueError, ValidationError):
                continue  # Reported when the call runs.
            requests.append((name, arguments))
        await self.host.preauthorize(requests)

    async def execute(self, call: dict, assistant_wire: dict) -> Any:
        name = call['function']['name']
        self._record_attempt(name)
        inv = ToolInvocation(name, None)
        result: dict[str, Any]
        self.emit(name, 'start')
        try:
            inv.arguments = json.loads(call['function']['arguments'])
            inv.arguments = self._normalized_arguments(name, inv.arguments)
            # A skill read earlier in this same round may change the image contract.
            # Validate the envelope here; save the draft body even when incomplete or invalid
            # so the domain validator can report every issue and support targeted repairs.
            envelope = inv.arguments
            if name == 'illustration_draft' and isinstance(inv.arguments, dict) and isinstance(inv.arguments.get('changes'), dict):
                envelope = inv.arguments | {'changes': {}}
            validate_call(self.definitions(), name, envelope)
            result = await self._run(inv)
            self.sources.extend(self.host.sources(name, result))
            self.emit(name, 'complete', result)
        except asyncio.CancelledError:
            if name == 'document_create' and self.on_images:
                self.on_images(await self.host.attachments_for(self.assistant.id))
            raise
        except RecoveryRequired as error:
            self.emit(name, 'error', {'error': str(error), 'notice': 'Image saved; recovery is required.'})
            raise
        except Exception as error:
            result = self._failure_result(inv, error)
            self.emit(name, 'error', result)
        serialized = json.dumps(result, ensure_ascii=False)
        if len(serialized) > MAX_TOOL_RESULT_CHARS:
            serialized = json.dumps({'error': 'Tool output exceeded the context budget; narrow the query'})
        if self.citation_audit is not None:
            # Capture the exact accepted result, never the discarded oversized one.
            self.citation_audit.registry.capture(name, json.loads(serialized))
            if name in ('knowledge_search', 'knowledge_open', 'web_search', 'fetch_content'):
                await self.citation_audit.checkpoint()
        self.prompt.messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'name': name, 'content': serialized})
        return inv.generated

    def _record_attempt(self, name: str) -> None:
        """Remember that the model attempted a tool, even when the call later fails."""
        if name in ANIMATION_ATTEMPT_TOOLS:
            self.animation_attempted = True
        elif name == 'document_create':
            self.document_attempted = True
        elif name == 'illustration_draft':
            self.draft_attempted = True
        elif name == 'image_gen' and self.prompt.xiaohei:
            self.illustration_requested = True

    def _normalized_arguments(self, name: str, arguments: Any) -> Any:
        """Repair harmless argument shapes before validation; the domain validator still decides."""
        if name == 'read_skill' and isinstance(arguments, dict) and arguments.get('path') == '':
            arguments = {**arguments, 'path': 'SKILL.md'}
        if name == 'animation_create':
            arguments = normalize_arguments(arguments)
            if self.animation_repair_id is not None and isinstance(arguments, dict):
                arguments.setdefault('revises_attachment_id', self.animation_repair_id)
        return arguments

    async def _run(self, inv: ToolInvocation) -> dict:
        """Route a validated call to its handler. Unlisted names fail before any effect."""
        if inv.name in HANDLERS:
            return await getattr(self, HANDLERS[inv.name])(inv)
        if inv.name in FILE_TOOL_NAMES:
            return await self._file_tool(inv)
        if inv.name in CONTROLLER_TOOLS:
            return await self._controller_tool(inv)
        raise ValueError(f'Tool {inv.name!r} has no handler')

    async def _activate_tools(self, inv: ToolInvocation) -> dict:
        arguments = inv.arguments
        capability = arguments['capability']
        if capability == 'animations':
            self.prompt.enable_animations()
        elif capability == 'interactive_ui':
            self.prompt.enable_ui(arguments.get('profile', 'mixed'))
        else:
            self.prompt.enable_library_write()
        result = {'capability': capability, 'enabled': True,
                  'notice': 'Capability tools and instructions are now available. Continue the request.'}
        if capability == 'interactive_ui':
            result['notice'] = 'Emit the next answer as NDJSON semantic blocks, one block per line. No further tool calls in that segment.'
        return result

    async def _knowledge_add(self, inv: ToolInvocation) -> dict:
        return await self.host.propose_library_add(
            assistant=self.assistant, web_captures=self.web_captures, **inv.arguments)

    async def _illustration_draft(self, inv: ToolInvocation) -> dict:
        self.draft_attempted = True
        result = self.drafts.save(**inv.arguments)
        self.draft_valid = result['valid']
        return result

    async def _animation_plan(self, inv: ToolInvocation) -> dict:
        from .motion_plan import validate_plan
        arguments = inv.arguments
        if self.motion_plan_disabled:
            raise ValueError('Plan-first is unavailable for this reply; use animation_create directly')
        self.motion_plan_started = time.monotonic()
        self.rejected_motion_plan = None
        self.motion_plan_error = None
        self.motion_plan, duration = validate_plan(arguments['plan'])
        log.info('Animation plan validated for message %s; duration %.2fs', self.assistant.id, duration)
        return {'valid': True, 'duration': duration,
                'notice': 'Implement this plan with animation_create_planned; '
                          'use animation_create directly if its motion is unsupported.'}

    async def _animation_create_planned(self, inv: ToolInvocation) -> dict:
        from .motion_plan import assemble_source
        arguments = inv.arguments
        if self.motion_plan is None or self.motion_plan_disabled:
            raise ValueError('No accepted plan is available; use animation_create directly')
        self.animation_attempted = True
        try:
            source, duration = assemble_source(self.motion_plan, arguments['scene']['source'])
            scene = {**arguments['scene'], 'source': source, 'duration': duration}
            direct = normalize_arguments({'scene': scene, 'loop': arguments['loop']})
            kwargs = {'on_progress': lambda phase: self.emit(inv.name, phase),
                      'expected_duration': duration, 'plan': self.motion_plan}
            if callback := self.visual_review_callback(inv.name):
                kwargs['visual_review'] = callback
            result, saved = await self.host.create_animation(self.assistant, direct, **kwargs)
        except (ValueError, ValidationError):
            self.motion_plan_disabled = True
            raise
        self.animation_created = True
        self.report_visual_review(result)
        self.report_motion_check(result)
        self.prompt.animation_ids.add(result['attachment_id'])
        result['strategy'] = 'plan-first'
        missing_labels = [element['id'] for element in self.motion_plan['elements']
                          if element.get('label') and element['label'] not in arguments['scene']['source']]
        elements = (result.get('motion') or {}).get('elements') or {}
        result['conformance'] = {'timeline': ('review-repaired source' if result.get('visual_review', {}).get('repaired')
                                               else 'host-generated'),
            'elements': elements.get('assessment', 'unknown'),
            'labels': 'review_required' if missing_labels else 'source_mentions_labels',
            'visual_layout': 'unreviewed'}
        if elements.get('issues'):
            result['conformance']['element_issues'] = elements['issues'][:4]
        if missing_labels:
            result['conformance']['label_review_elements'] = missing_labels
        log.info('Plan-first animation saved for message %s in %.2fs', self.assistant.id,
                 time.monotonic() - (self.motion_plan_started or time.monotonic()))
        if self.on_animations:
            self.on_animations(saved)
        return result

    async def _animation_create(self, inv: ToolInvocation) -> dict:
        kwargs = {'on_progress': lambda phase: self.emit(inv.name, phase)}
        if callback := self.visual_review_callback(inv.name):
            kwargs['visual_review'] = callback
        result, saved = await self.host.create_animation(self.assistant, inv.arguments, **kwargs)
        self.animation_created = True
        self.report_visual_review(result)
        self.report_motion_check(result)
        result['strategy'] = 'direct'
        self.prompt.animation_ids.add(result['attachment_id'])
        if self.animation_repair_id is not None:
            # Record the replacement before publishing it to the UI. A
            # crash/cancellation after save must not regenerate it.
            await self.host.finish_animation_repair(self.assistant.conversation_id,
                self.animation_repair_id, 'replaced', result['attachment_id'])
        if self.on_animations:
            self.on_animations(saved)
        return result

    async def _file_tool(self, inv: ToolInvocation) -> dict:
        result = await self.host.file_tool(inv.name, inv.arguments, self.assistant)
        if inv.name == 'document_create':
            self.document_created = True
            if self.on_images:
                self.on_images(await self.host.attachments_for(self.assistant.id))
        return result

    async def _run_skill_script(self, inv: ToolInvocation) -> dict:
        if self.script_attempts >= 4:
            raise ValueError('At most four skill script attempts per reply')
        self.script_attempts += 1
        result = await self.host.run_skill_script(self.assistant, inv.arguments)
        if 'artifact' in result and self.on_images:
            self.on_images(await self.host.attachments_for(self.assistant.id))
        return result

    async def _image_gen(self, inv: ToolInvocation) -> dict:
        arguments = inv.arguments
        if self.prompt.xiaohei:
            draft = self.drafts.validated(arguments['draft_id'], arguments['revision'])
            compiled = prepare_illustration(draft)
            self.on_delta(plan_markdown(draft))
            self.emit(inv.name, 'progress', {'message': 'Draft validated · generating illustration…'})
            draft_identity = {k: arguments[k] for k in ('draft_id', 'revision')}
            arguments = {k: v for k, v in arguments.items() if k not in ('draft_id', 'revision')}
            arguments['prompt'] = compiled
            inv.arguments = arguments
        def image_start():
            self.effects.start_image()
            inv.image_started = True
        result, saved, generated = await self.host.generate_image(
            self.assistant, arguments,
            lambda message: self.emit(inv.name, 'progress', {'message': message}),
            on_start=image_start)
        inv.generated = generated
        if self.prompt.xiaohei:
            self.illustration_created = True
            result.update(draft_identity)
            result['qa'] = {'file_integrity': 'passed',
                            'aspect_ratio': ('passed' if result.get('width', 0) * 9 ==
                                             result.get('height', 0) * 16 and result.get('width')
                                             else 'review_required'),
                            'facts': 'unverified', 'visual_quality': 'unreviewed',
                            'labels': 'unreviewed', 'character': 'unreviewed'}
        if self.on_images:
            self.on_images(saved)
        if getattr(generated, 'cancelled', False):
            raise asyncio.CancelledError
        if getattr(generated, 'cleanup_warning', None):
            raise RecoveryRequired(generated.cleanup_warning)
        return result

    async def _controller_tool(self, inv: ToolInvocation) -> dict:
        name, arguments = inv.name, inv.arguments
        result = await self.host.call_tool(name, arguments)
        if name == 'fetch_content' and 'text' in result and self.host.knowledge_enabled:
            reference = 'web:' + secrets.token_hex(16)
            self.web_captures[reference] = self.host.web_capture(result, self.assistant)
            result = {**result, 'source_ref': reference}
        # Host contracts follow the identity of a named bundled skill whose
        # instructions were read, never whatever text a skill contains.
        # Local models only get authoring from a user-selected skill; a
        # model-chosen skill read is not manual.
        if (name == 'read_skill' and result.get('skill_id') == ANIMATION_SKILL_ID
                and result.get('path') == 'SKILL.md'
                and not self.host.animations_manual_only):
            self.prompt.enable_animations()
        if (name == 'read_skill' and result.get('skill_id') == SKILL_ID
                and result.get('path') == 'SKILL.md'):
            self.prompt.xiaohei = True
            result['generation_contract'] = CONTRACT
        return result

    def _failure_result(self, inv: ToolInvocation, error: Exception) -> dict:
        """Shape a failed call's result, then let that tool's failure hook update turn state."""
        result: dict[str, Any] = {'error': str(error)[:1000], 'code': getattr(error, 'code', type(error).__name__), 'notice': 'Tool failed. This is not an empty result.'}
        if hook := FAILURE_HOOKS.get(inv.name):
            getattr(self, hook)(inv, error, result)
        return result

    def _fail_motion_plan(self, inv: ToolInvocation, error: Exception, result: dict) -> None:
        name, arguments = inv.name, inv.arguments
        self.motion_plan_disabled = True
        self.motion_plan = None
        if name == 'animation_plan':
            # Keep the rejected plan and its exact diagnostic for debugging.
            # The model still receives the error text and fallback notice.
            self.rejected_motion_plan = (arguments.get('plan')
                                         if isinstance(arguments, dict) else None)
            self.motion_plan_error = str(error)[:2000]
        result['notice'] = ('Plan-first failed; use animation_create directly with a complete '
                            'Motion Canvas source. Do not retry planning in this reply.')
        log.info('Plan-first fallback for message %s at %s (%s): %s',
                 self.assistant.id, name, type(error).__name__, str(error)[:1000])
        self.animation_error = result['error']

    def _fail_destination_request(self, inv: ToolInvocation, error: Exception, result: dict) -> None:
        # Keep failed and refused requests visible beside their destination.
        arguments = inv.arguments
        if isinstance(arguments, dict):
            result['request'] = {'kind': 'search' if inv.name == 'web_search' else 'fetch',
                                 'target': str(arguments.get('query') or arguments.get('url') or '')[:2000]}
            if isinstance(error, OutboundDenied):
                result['notice'] = 'Not sent. Continue without this request, or ask the user in your answer.'

    def _fail_document(self, inv: ToolInvocation, error: Exception, result: dict) -> None:
        self.document_error = result['error']

    def _fail_animation_create(self, inv: ToolInvocation, error: Exception, result: dict) -> None:
        arguments = inv.arguments
        self.animation_error = result['error']
        if isinstance(error, (ValueError, ValidationError)):
            try:
                details = repair_details(arguments, error)
            except Exception as feedback_error:
                log.warning('Could not build animation repair details for message %s: %s',
                            self.assistant.id, feedback_error)
                details = {'validation_errors': [{'path': 'arguments',
                            'message': 'Animation arguments could not be inspected. Correct the '
                                       'scene shape and retry.'}]}
            result.update(details)
            if result['validation_errors']:
                # Keep both the validator's first failure and its richer repair
                # detail, which may pinpoint a nested field more precisely.
                result['original_error'] = result['error']
                result['error'] = '; '.join(
                    f"{issue['path']}: {issue['message']}"
                    for issue in result['validation_errors'][:3])[:1000]
                self.animation_error = result['error']
            result['notice'] = (
                'Animation specification rejected before saving. Correct ALL validation_errors '
                'and call animation_create again. Put title, description, duration, nodes and '
                'tracks INSIDE scene. The app supplies version=1 when omitted. Keep loop beside '
                'scene. Do not remove required scene metadata. Use one track per node/property, '
                'combining all highlight phases into strictly increasing keyframes. Pass '
                'top-level loop=true for looping. For a NEW animation omit revises_attachment_id. '
                'Do not stop at describing the error; retry within the remaining tool rounds.')
            scene = arguments.get('scene') if isinstance(arguments, dict) else None
            if isinstance(scene, dict) and 'source' in scene:
                result['notice'] = ('Fix the reported Motion Canvas source or metadata error and retry '
                    'animation_create. Supply the complete TSX module in scene.source; '
                    'keep loop beside scene. Do not stop at describing the error.')
        log.warning('Animation creation failed for message %s: %s', self.assistant.id, self.animation_error)

    def _fail_image_gen(self, inv: ToolInvocation, error: Exception, result: dict) -> None:
        if isinstance(error, OutboundDenied):
            # Refused before any image job; the pre-start notice is not wanted here.
            result['request'] = {'kind': 'image', 'target': error.request.target[:2000]}
            result['notice'] = ('Not sent; no image job started. Continue without the image, '
                                'or ask the user in your answer.')
        elif not inv.image_started:
            result['notice'] = ('Image specification rejected before generation. Correct the '
                                'tool arguments and try again; no image job has started.')
            if not self.effects.images_available:
                result['notice'] = 'Further image execution is blocked for this reply.'
        else:
            self.effects.image_error = result['error']
            result['notice'] = ('Image generation failed. Report this to the user. '
                                'Do not retry or switch to paid generation unless asked. '
                                'Further image execution is blocked for this reply.')
            if recovery := self.host.image_recovery_error():
                raise RecoveryRequired(recovery) from error
