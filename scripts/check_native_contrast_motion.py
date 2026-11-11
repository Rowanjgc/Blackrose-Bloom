"""Observe rendered foreground/background samples and reduced motion natively.

Run under the private AT-SPI/Xvfb wrapper. It creates only synthetic transcript
data and private XDG state. GTK's animation preference is changed only on this
process's Gtk.Settings object, with GSettings using its memory backend. The
probe never opens providers or user conversations. Pixel-derived ratios are
bounded observations, not WCAG certification.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from test_gui_images import make_runtime  # noqa: E402


def rgba(value: str) -> tuple[tuple[int, int, int], float]:
    """Parse the WebKit computed rgb()/rgba() serialization into a colour and its alpha."""
    if value.startswith('#'):
        return tuple(bytes.fromhex(value[1:7])), 1.0  # type: ignore[return-value]
    left, _, inside = value.partition('(')
    if left not in ('rgb', 'rgba') or not inside.endswith(')'):
        raise ValueError(f'Unsupported computed color: {value}')
    pieces = inside[:-1].split(',')
    alpha = float(pieces[3].strip()) if len(pieces) > 3 else 1.0
    return tuple(round(float(channel.strip())) for channel in pieces[:3]), alpha  # type: ignore[return-value]


def rgb(value: str) -> tuple[int, int, int]:
    """An opaque computed colour; translucent ones need a background (see composite)."""
    color, alpha = rgba(value)
    if alpha < 1:
        raise ValueError(f'Computed foreground is translucent: {value}')
    return color


def composite(value: str, background: tuple[int, int, int]) -> tuple[int, int, int]:
    """The colour a translucent foreground paints over ``background`` (source-over)."""
    color, alpha = rgba(value)
    return tuple(round(alpha * front + (1 - alpha) * back)  # type: ignore[return-value]
                 for front, back in zip(color, background, strict=True))


def luminance(color: tuple[int, int, int]) -> float:
    def linear(value):
        value = value / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (linear(channel) for channel in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: tuple[int, int, int], background: tuple[int, int, int]) -> float:
    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def comparison_block_reason(target: dict) -> str | None:
    if not target.get('enabled', True):
        return 'disabled control contrast is not included'
    # The page reports visualEffects; visual_effects is kept for older reports.
    if target.get('visualEffects') or target.get('visual_effects'):
        return 'target or ancestor opacity/filter/blend changes rendered foreground'
    return None


async def seed(root: Path):
    config, runtime = make_runtime(root / 'runtime')
    await runtime.open()
    controller = runtime.controller
    conversation = await controller.new_conversation()
    await controller._repo.add_message(
        conversation.id, 'user',
        'Synthetic sample question: compare the printed leaf with the dark page.')
    await controller._repo.add_message(
        conversation.id, 'assistant',
        'A quiet reading sample uses real transcript text.\n\n'
        'The page keeps one **primary action** and secondary status details visible.\n\n'
        'A final short line leaves a clean background area for pixel sampling.')
    await runtime.database.close()
    return config, runtime


def dominant_pixel_color(pixbuf, rect: dict, scale: float) -> tuple[tuple[int, int, int], int, list[int]]:
    width, height = pixbuf.get_width(), pixbuf.get_height()
    x0 = max(0, round(rect['x'] * scale))
    y0 = max(0, round(rect['y'] * scale))
    x1 = min(width, round((rect['x'] + rect['width']) * scale))
    y1 = min(height, round((rect['y'] + rect['height']) * scale))
    if x1 <= x0 or y1 <= y0:
        raise ValueError('Rendered surface sample box is empty')
    channels, rowstride = pixbuf.get_n_channels(), pixbuf.get_rowstride()
    data = pixbuf.get_pixels()
    histogram = {}
    for py in range(y0, y1):
        for px in range(x0, x1):
            offset = py * rowstride + px * channels
            color = tuple(data[offset + index] for index in range(3))
            histogram[color] = histogram.get(color, 0) + 1
    color, count = max(histogram.items(), key=lambda item: item[1])
    return color, count, [x0, y0, x1, y1]


def setup_private_desktop(root: Path) -> dict:
    config_home = root / 'config'
    config_home.mkdir(parents=True)
    for variable, folder in (
            ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data'),
            ('XDG_STATE_HOME', 'state'), ('XDG_CACHE_HOME', 'cache'),
            ('XDG_RUNTIME_DIR', 'runtime-dir')):
        path = root / folder
        path.mkdir(mode=0o700, exist_ok=True)
        os.environ[variable] = str(path)
    os.environ.pop('DCONF_PROFILE', None)
    os.environ['GSETTINGS_BACKEND'] = 'memory'
    return {'gsettings_backend': 'memory',
            'persistent_dconf_mutation': False,
            'gtk_setting_scope': 'current process Gtk.Settings object only'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=150)
    args = parser.parse_args()
    if not 30 <= args.timeout <= 300:
        parser.error('--timeout must be from 30 to 300 seconds')
    if os.environ.get('BLACKROSE_PRIVATE_ATSPI') != '1':
        parser.error('Use dbus-run-session -- bash scripts/run_private_atspi.sh ...')

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    screenshot_100 = output.with_name(output.stem + '-100.png')
    screenshot_200 = output.with_name(output.stem + '-200.png')
    report = {
        'recorded_at': datetime.now(UTC).isoformat(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'limitations': [
            'Contrast values combine computed CSS foreground colors with the modal rendered pixel color inside a selected visible surface rectangle.',
            'This is a bounded spot check, not WCAG certification or a full control/state audit.',
            'Synthetic fixture and private Xvfb do not establish color appearance on physical displays or all desktop themes.',
            'No human low-vision or assistive-technology review was performed.',
            'The background mode summarizes the dominant rendered pixel tone; it does not characterize texture, gradients, or local variations.',
            'The textarea placeholder is shown in the screenshot but excluded from contrast scores because this WebKit build returned the textarea color when queried for the placeholder pseudo-element.',
        ],
        'synthetic_fixture_only': True,
        'providers_or_user_data_used': False,
        'screenshot_100': str(screenshot_100),
        'screenshot_200': str(screenshot_200),
    }
    errors = []

    with tempfile.TemporaryDirectory(prefix='blackrose-contrast-motion-') as directory:
        root = Path(directory)
        report['private_motion_setting'] = setup_private_desktop(root)
        import webview
        from blackrose.gui.app import run_gui

        original_create = webview.create_window
        gtk_thread_id = threading.get_ident()
        config, runtime = asyncio.run(seed(root))

        def create(*positional, **kwargs):
            window = original_create(*positional, **kwargs)
            checked = False

            def loaded():
                nonlocal checked
                if checked:
                    return
                checked = True

                def evaluate(script):
                    return window.evaluate_js(script)

                def wait(script, seconds=35):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        try:
                            if evaluate(script):
                                return
                        except Exception:
                            pass
                        time.sleep(.04)
                    raise TimeoutError('Timed out waiting for ' + script)

                def gtk_call(operation, timeout=8):
                    """Run GTK/GDK operations on the thread that enters webview.start."""
                    if threading.get_ident() == gtk_thread_id:
                        return operation()
                    from gi.repository import GLib
                    complete = threading.Event()
                    result = []

                    def invoke():
                        try:
                            result.append((True, operation()))
                        except BaseException as error:
                            result.append((False, error))
                        finally:
                            complete.set()
                        return GLib.SOURCE_REMOVE

                    GLib.idle_add(invoke)
                    if not complete.wait(timeout):
                        raise TimeoutError('GTK main thread did not complete the scheduled observation')
                    succeeded, value = result[0]
                    if not succeeded:
                        raise value
                    return value

                def capture(path: Path, descriptors: dict):
                    time.sleep(.25)  # let WebKit paint the changed scale first

                    def on_gtk():
                        from gi.repository import Gdk, Gtk, WebKit2
                        native_window = window.native.get_window()
                        width, height = descriptors['viewport']['width'], descriptors['viewport']['height']
                        pixbuf = Gdk.pixbuf_get_from_window(native_window, 0, 0, width, height)
                        if pixbuf is None:
                            raise RuntimeError('GDK could not capture the rendered private window')
                        path.parent.mkdir(parents=True, exist_ok=True)
                        pixbuf.savev(str(path), 'png', [], [])
                        samples = []
                        for item in descriptors['targets']:
                            block_reason = comparison_block_reason(item)
                            if block_reason:
                                samples.append({**item, 'contrast_ratio_from_sample': None,
                                                'sample_unavailable_reason': block_reason})
                                continue
                            if not item['text_visible']:
                                samples.append({**item, 'contrast_ratio_from_sample': None,
                                                'sample_unavailable_reason': 'target text not visible in captured viewport'})
                                continue
                            if item['sampleSurfaceRect']['width'] <= 0 or item['sampleSurfaceRect']['height'] <= 0:
                                samples.append({**item, 'contrast_ratio_from_sample': None,
                                                'sample_unavailable_reason': 'surface is outside the captured viewport'})
                                continue
                            ratio = descriptors['devicePixelRatio']
                            background, count, box = dominant_pixel_color(
                                pixbuf, item['sampleSurfaceRect'], ratio)
                            foreground = composite(item['foreground'], background)
                            samples.append({
                                **item,
                                'foreground_painted_rgb': list(foreground),
                                'background_sample_rgb': list(background),
                                'sample_box_pixel': box,
                                'background_mode_pixel_count': count,
                                'contrast_ratio_from_sample': round(contrast_ratio(foreground, background), 2),
                                'wcag_threshold_reference': '4.5:1 normal text; 3:1 large text (reference only, no conformance claim)',
                            })
                        versions = {
                            'gtk': '.'.join(str(part) for part in (
                                Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version())),
                            'webkit': '.'.join(str(part) for part in (
                                WebKit2.get_major_version(), WebKit2.get_minor_version(), WebKit2.get_micro_version())),
                        }
                        return {'pixel_size': [pixbuf.get_width(), pixbuf.get_height()],
                                'versions': versions, 'samples': samples}

                    native = gtk_call(on_gtk)
                    report['native_versions'] = native['versions']
                    report['screenshot_pixel_size'] = native['pixel_size']
                    return {'measurement': descriptors['measurement'],
                            'viewport': descriptors['viewport'],
                            'targets': native['samples']}

                try:
                    wait("document.querySelector('#status-text')?.textContent === 'Ready'")
                    wait("document.querySelector('.msg.assistant .body p')")
                    def set_private_gtk_motion():
                        from gi.repository import Gtk
                        gtk_settings = Gtk.Settings.get_default()
                        before = bool(gtk_settings.get_property('gtk-enable-animations'))
                        gtk_settings.set_property('gtk-enable-animations', False)
                        after = bool(gtk_settings.get_property('gtk-enable-animations'))
                        return before, after

                    before, after = gtk_call(set_private_gtk_motion)
                    report['gtk_settings'] = {
                        'gtk_enable_animations_before': before,
                        'gtk_enable_animations_after': after,
                        'source': 'process-local Gtk.Settings property; GSettings backend is memory',
                    }
                    all_targets = [
                        ['assistant body text', '.msg.assistant .body p', '.column', None],
                        ['user transcript text', '.msg.user .bubble', '.column', None],
                        ['toolbar enabled control', '#new-chat', '#app-toolbar', None],
                        ['primary enabled control', '#send', '#send', None],
                        ['secondary status text', '#status-text', 'body', None],
                        ['composer placeholder text', '#input', '#input', '::placeholder'],
                    ]

                    def describe(measurement, names=None):
                        chosen = [target for target in all_targets if names is None or target[0] in names]
                        return evaluate(f'''(() => {{
                          const targets = {json.dumps(chosen)};
                          const rows = targets.flatMap(([name, selector, surfaceSelector, pseudo]) => {{
                            const element = document.querySelector(selector);
                            const surface = document.querySelector(surfaceSelector);
                            if (!element || !surface) return [];
                            const rect = element.getBoundingClientRect();
                            const surfaceRect = surface.getBoundingClientRect();
                            let left=Math.max(0,surfaceRect.left), top=Math.max(0,surfaceRect.top);
                            let right=Math.min(innerWidth,surfaceRect.right), bottom=Math.min(innerHeight,surfaceRect.bottom);
                            for (let parent=surface.parentElement; parent; parent=parent.parentElement) {{
                              const overflow=getComputedStyle(parent).overflow;
                              if (/auto|scroll|hidden|clip/.test(overflow)) {{
                                const clip=parent.getBoundingClientRect();
                                left=Math.max(left,clip.left); top=Math.max(top,clip.top);
                                right=Math.min(right,clip.right); bottom=Math.min(bottom,clip.bottom);
                              }}
                            }}
                            const sampleSurfaceRect={{x:left,y:top,width:Math.max(0,right-left),height:Math.max(0,bottom-top)}};
                            let targetLeft=Math.max(0,rect.left), targetTop=Math.max(0,rect.top);
                            let targetRight=Math.min(innerWidth,rect.right), targetBottom=Math.min(innerHeight,rect.bottom);
                            for (let parent=element.parentElement; parent; parent=parent.parentElement) {{
                              const overflow=getComputedStyle(parent).overflow;
                              if (/auto|scroll|hidden|clip/.test(overflow)) {{
                                const clip=parent.getBoundingClientRect();
                                targetLeft=Math.max(targetLeft,clip.left); targetTop=Math.max(targetTop,clip.top);
                                targetRight=Math.min(targetRight,clip.right); targetBottom=Math.min(targetBottom,clip.bottom);
                              }}
                            }}
                            let textVisible=false;
                            if (targetRight>targetLeft && targetBottom>targetTop) {{
                              const hit=document.elementFromPoint((targetLeft+targetRight)/2,(targetTop+targetBottom)/2);
                              textVisible=hit===element || element.contains(hit);
                            }}
                            const style = getComputedStyle(element, pseudo);
                            // WebKit's getComputedStyle ignores ::placeholder, so read the
                            // matching rule and resolve its custom property.
                            let foreground = style.color, foregroundSource = 'computed style';
                            if (pseudo === '::placeholder') {{
                              for (const sheet of document.styleSheets) {{
                                let rules; try {{ rules = sheet.cssRules; }} catch (error) {{ continue; }}
                                for (const rule of rules) {{
                                  const parts = (rule.selectorText || '').split(',').map(part => part.trim());
                                  if (!parts.some(part => part.endsWith('::placeholder') &&
                                      element.matches(part.slice(0, -'::placeholder'.length)))) continue;
                                  let value = rule.style.color;
                                  const custom = /^var\((--[^),]+)\)$/.exec(value);
                                  if (custom) value = getComputedStyle(element).getPropertyValue(custom[1]).trim();
                                  if (value) {{ foreground = value; foregroundSource = 'stylesheet ' + rule.selectorText; }}
                                }}
                              }}
                            }}
                            const visualEffects=[];
                            for (let current=element; current && current!==document.body; current=current.parentElement) {{
                              const currentStyle=getComputedStyle(current);
                              const opacity=Number(currentStyle.opacity);
                              const selectorName=current.id?'#'+current.id:(typeof current.className==='string'&&current.className?'.'+current.className.split(/\\s+/).join('.'):current.tagName);
                              if (opacity<.999) visualEffects.push({{selector:selectorName,opacity}});
                              if (currentStyle.filter!=='none') visualEffects.push({{selector:selectorName,filter:currentStyle.filter}});
                              if (currentStyle.mixBlendMode!=='normal') visualEffects.push({{selector:selectorName,blend:currentStyle.mixBlendMode}});
                            }}
                            return [{{name, selector, foreground, foregroundSource,
                              fontSize: style.fontSize, fontWeight: style.fontWeight,
                              backgroundCss: getComputedStyle(element).backgroundColor,
                              rect: {{x:rect.x,y:rect.y,width:rect.width,height:rect.height}},
                              sampleSurface: surfaceSelector,
                              sampleSurfaceRect,
                              text_visible:textVisible,
                              visualEffects,
                              enabled: !element.disabled}}];
                          }});
                          return {{measurement:{json.dumps(measurement)},
                            viewport:{{width:innerWidth,height:innerHeight}},
                            devicePixelRatio, reducedMotionMediaQuery:matchMedia('(prefers-reduced-motion: reduce)').matches,
                            rootFontSize:getComputedStyle(document.documentElement).fontSize,
                            document:{{scrollWidth:document.documentElement.scrollWidth,clientWidth:document.documentElement.clientWidth}},
                            targets:rows}};
                        }})()''')

                    initial_scale = evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
                    base = describe('100-percent root text')
                    report['reduced_motion_observation'] = {
                        'gtk_enable_animations': after,
                        'match_media_prefers_reduced_motion': base['reducedMotionMediaQuery'],
                    }
                    report['text_scale_100_percent'] = capture(screenshot_100, base)
                    evaluate(f"""(() => {{
                      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
                      document.documentElement.style.fontSize='{initial_scale * 2}px';
                      requestAnimationFrame(() => requestAnimationFrame(() =>
                        document.querySelector('#transcript').scrollTop=0));
                    }})()""")
                    wait(f"getComputedStyle(document.documentElement).fontSize === '{initial_scale * 2}px'")
                    time.sleep(.25)
                    scaled = describe('200-percent root text size')
                    # At 200% the panes scroll, so each sample is measured where a
                    # reader scrolls it into view.
                    scrolled_200 = []
                    for index, (name, selector, _surface, _pseudo) in enumerate(all_targets):
                        evaluate(f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block:'center'}})")
                        time.sleep(.3)
                        view = describe('200-percent root text size, scrolled into view', [name])
                        scrolled_200.append(capture(output.with_name(f"{output.stem}-200-{index}.png"), view))
                    scaled['scrollState'] = evaluate("({windowY:scrollY,transcriptTop:document.querySelector('#transcript').scrollTop,transcriptHeight:document.querySelector('#transcript').clientHeight,transcriptScrollHeight:document.querySelector('#transcript').scrollHeight})")
                    report['text_scale_200_percent'] = {
                        'base_root_font_px': initial_scale,
                        'scaled_root_font_px': float(scaled['rootFontSize'].removesuffix('px')),
                        'root_font_ratio': round(float(scaled['rootFontSize'].removesuffix('px')) / initial_scale, 2),
                        'document_horizontal_overflow_px': scaled['document']['scrollWidth'] - scaled['document']['clientWidth'],
                        'measurements': capture(screenshot_200, scaled),
                        'scrolled_into_view': scrolled_200,
                    }
                    report['observation_completed'] = (report['text_scale_200_percent']['root_font_ratio'] == 2.0
                                                       and report['gtk_settings']['gtk_enable_animations_after'] is False
                                                       and len(base['targets']) >= 4)
                except BaseException as error:
                    errors.append(error)
                    report['error'] = f'{type(error).__name__}: {error}'
                    traceback.print_exc()
                finally:
                    output.write_text(json.dumps(report, indent=2) + '\n')
                    window.destroy()

            window.events.loaded += loaded
            return window

        webview.create_window = create
        try:
            run_gui(config, runtime=runtime)
        finally:
            webview.create_window = original_create
    if errors:
        raise SystemExit(1)
    print(f'Native contrast/motion observations: {output}; completed={report.get("observation_completed")}', flush=True)
    if not report.get('observation_completed'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
