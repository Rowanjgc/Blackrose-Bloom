# Gothic Bloom — Unified Design Language

**A quiet midnight garden, with precise jewel accents.**

Gothic Bloom is a reusable visual and interaction language for websites, applications, and editorial experiences. It combines botanical-gothic identity with modern clarity: ink-dark surfaces, rose silhouettes, pointed arches, refined typography, and responsive, familiar behavior.

The intended feeling is **calm confidence with a trace of mystery**. It should feel elegant and distinctive without making everyday tasks theatrical.

> Gothic in silhouette. Botanical in detail. Luxurious in restraint. Fluid in behavior.

This document defines the canonical, unified style. It is a reference for new projects, not a claim that existing implementations meet every requirement.

## 1. One Identity, Not Five Themes

The original collections contribute strengths to a single system:

| Source | Retained quality | Unified expression |
| --- | --- | --- |
| Classic Midnight Garden | Calm botanical foundation | Ink-dark surfaces, quiet navigation, stable reading areas |
| Crimson Witch | Decisive hierarchy | Clear primary actions and restrained rose emphasis |
| Royal Luxe | Precision and richness | Refined typography, generous spacing, rare gold details |
| Mystical | Soft luminosity | A subtle halo around an occasional featured element |
| Ethereal | Space and architectural depth | Negative space, sparse texture, restrained arch geometry |

These qualities coexist through hierarchy, not through accumulating motifs. Do not combine crowns, crystals, constellations, vines, thorns, and roses into a single composition.

The canonical identity is the **rose and the pointed arch**. Other motifs are optional editorial content, not a second design system.

## 2. Principles

### Clarity before atmosphere
A person should understand the page, its primary task, and its next action before noticing the ornament. Essential labels are literal: “Library,” “Save,” “Activity,” “Settings.” Poetic language belongs in titles and supporting copy.

### Ornament marks significance
A rose identifies the brand. An arch frames a featured area. A vine quietly marks an edge. Tables, forms, dense charts, and routine controls remain undecorated.

### Luxury is precision
Richness comes from proportion, spacing, typography, and carefully placed highlights—not from excessive gold, glow, blur, or ornament.

### Depth explains function
Reading surfaces are stable and near-opaque. Translucent surfaces belong to floating navigation and utilities. A modal uses a denser surface and a scrim because it asks for focused attention.

### Behavior supports agency
Feedback is immediate, motion remains interruptible, and every task has an understandable exit. Preserve familiar controls and keyboard behavior. Provide undo where practical; reserve confirmations for genuinely destructive actions.

### One system across contexts
Marketing pages, dashboards, libraries, and editors share the same type roles, spacing, color semantics, control anatomy, and interaction behavior. Adapt density and composition to the task without inventing a new aesthetic.

## 3. Color System

### Foundation palette

| Name | Value | Role |
| --- | --- | --- |
| Umbral Black | `#0D0D0D` | Canvas and deepest background |
| Witching Hour | `#121212` | Default reading surfaces |
| Raised Ink | `#1A1A1A` | Elevated opaque surfaces and neutral interaction fills |
| Ghost White | `#E0E0E0` | Primary text and important values |
| Silver Vine | `#B3B3B3` | Supporting text and metadata |
| Thorn Gray | `#333333` | Decorative edges and subtle separators |
| Black Cherry | `#B8506B` | Brand identity and botanical accents |
| Ruddy Rose | `#EF6461` | Primary actions and selected emphasis |
| Queen’s Jewel | `#D4AF37` | Rare editorial or featured highlights |
| Ivy Green | `#5E8C61` | Botanical illustration and optional labeled data accents |

### Accent hierarchy

**Cherry is the identity; rose is the action; gold is the exception.**

- Keep the overwhelming majority of surface area neutral.
- Use rose consistently for the primary action and meaningful selection, not for every link, value, and icon.
- Use cherry in the brand and restrained botanical details. Do not assume it is suitable for small text.
- Reserve gold for a featured insight, small editorial marker, or premium detail. Never outline every card in gold.
- Use ivy only where it serves content. It is not a second default action color.
- Prefer neutral secondary buttons over introducing another accent.

A useful composition heuristic is roughly **90% neutral, 8% rose/cherry, and 2% exceptional detail**. This is an attention budget, not a measurable acceptance requirement.

### Semantic colors are separate
Do not make the brand palette stand in for every status. Errors, warnings, success, and information need dedicated semantic tokens, labels, and icons. Rose does not automatically mean danger; ivy does not automatically mean success.

Charts use a deliberate data palette, independent of button semantics. Label series directly where possible and supplement color with line styles, patterns, or symbols.

### Contrast requirements
Verify rendered foreground/background combinations, including opacity, hover, focus, disabled states, and composited materials:

- Normal text: at least **4.5:1**.
- Large text: at least **3:1**.
- Required control boundaries and graphical information: at least **3:1** against adjacent colors.

Thorn Gray is decorative; it is not a sufficient required control boundary on ink-dark surfaces. Use a separately tested stronger token for input edges and focus indicators. Dark text is a sensible starting point on solid rose or gold fills, but test the actual pairing.

## 4. Typography

Typography has two voices: **an effortless interface and a restrained editorial presence**.

- **Interface, body, and data:** platform system sans-serif.
- **Editorial headings:** one readable serif with controlled contrast and elegant proportions.
- **Wordmark only:** blackletter such as UnifrakturCook, when appropriate. Never use blackletter for navigation, forms, buttons, chart labels, or paragraphs.

Avoid introducing a separate decorative font for each section. Enable optical sizing when supported and tabular numerals for aligned data.

| Role | Size | Line height | Treatment |
| --- | --- | --- | --- |
| Editorial hero | `clamp(2.5rem, 6vw, 4.5rem)` | `1.05–1.15` | Serif; tracking tuned to the face |
| Page heading | `1.75–2.25rem` | `1.15–1.25` | Sans or editorial serif, consistently by context |
| Section heading | `1.125–1.25rem` | `1.3` | Medium or semibold |
| Body and controls | `1rem` | `1.5` | Regular body; medium controls |
| Supporting label | `0.875rem` | `1.4` | Clear, not excessively faint |

Use slightly negative tracking for large sans-serif headings, near-zero tracking for body text, and modest positive tracking only where small labels benefit. Avoid long uppercase passages and widely spaced navigation labels.

Use `rem` and `em`; allow wrapping and growth under enlarged text. Keep prose around 60–75 characters per line. Treat these sizes as a starting scale, not permission to truncate content.

## 5. Layout and Rhythm

Use an **8-point spacing scale: 0.5, 1, 1.5, 2, and 3rem** (8, 16, 24, 32, and 48px at the default font size). Round fluid insets to the same rhythm. Zero spacing and automatic centering remain valid. Keep the New chat button's Ctrl N shortcut label at its original compact padding of `0.2em 0.4em` as an intentional exception. Typography, borders, radii, and icon geometry do not need to follow the spacing scale.

- Place related controls close together; separate different tasks clearly.
- Align cards, headings, and working areas to a shared grid.
- Use generous outer space, not oversized padding in every dense component.
- Let content determine height. Avoid fixed card heights that clip labels or translated copy.
- Keep the main action near the content it affects.
- Preserve reading order on narrow screens rather than shrinking desktop ornament.

### Default page hierarchy

1. Location, title, context, and primary action.
2. Essential information or working content.
3. Featured insight or supporting detail.
4. Secondary activity and utilities.

For dashboards, metrics can precede the main chart. For libraries, search and filters precede results. For editors, the document is the main area. The style must adapt to the task rather than forcing every project into a dashboard.

## 6. Shape and Ornament

### Everyday shape family
Use softly rounded rectangles for interactive and reading surfaces. Suggested starting radii:

- Controls: `0.625rem`.
- Cards: `1rem`.
- Sheets and dialogs: `1.25rem`.
- Pills: only for tags, compact status, or an intentionally segmented control.

Use one radius family consistently. Do not mix sharp, scalloped, arched, and pill-shaped cards arbitrarily.

### The pointed arch
The arch is an architectural signature, not the default container shape. Use it for one featured composition, a hero illustration, or a brand frame. Keep text and interactive hit areas rectangular and unobstructed inside it.

On functional pages, start with **one featured arch at most**. On editorial pages, repetition may establish a rhythm, but should not turn every section into a shrine.

### The rose
Use a small, simplified rose silhouette for brand recognition. Reserve detailed rose artwork for large editorial illustrations. Do not stamp a rose on every button, metric, or empty state.

### Vines, thorns, and texture
Thin vines may live at section edges, away from reading areas. Thorns may mark an editorial boundary, never a required input border. Background texture should be static, sparse, and barely perceptible.

Decorative layers must be `aria-hidden="true"` where appropriate, ignore pointer input, reserve their dimensions, and never obscure text or focus rings. If decoration is removed, the layout must remain complete.

## 7. Materials and Light

| Layer | Default treatment |
| --- | --- |
| Canvas | Umbral Black; optional faint static botanical texture |
| Reading card | Witching Hour or Raised Ink; near-opaque fill; quiet edge |
| Floating navigation | Dark translucent material with moderate backdrop blur and a solid fallback |
| Popover | Compact raised surface, clear separation, origin anchored to its trigger |
| Modal or sheet | Dense surface, purposeful shadow, dimming scrim |
| Featured detail | Small solid accent; optional restrained halo outside the reading area |

Do not nest translucent panels. Never put dense data over an unstable glass background. Use blur only where content actually passes underneath a floating layer.

Shadows establish elevation, not decoration. Glows are exceptional brand details; never apply them to body text, every border, or every card. A scrolling edge can use a subtle gradient to separate content from floating chrome.

## 8. Component Language

### Navigation
Use familiar placement and literal labels. Selection combines a persistent marker, text emphasis, and an accessible current-page state. Rose may mark the active item; inactive items remain neutral. On mobile, preserve labels and hierarchy in a compact menu or sheet.

### Buttons
- **Primary:** solid rose fill with tested contrasting text.
- **Secondary:** neutral surface with a clearly visible boundary where needed.
- **Tertiary:** quiet text or icon action with adequate hit area.
- **Destructive:** distinct semantic treatment, not simply the primary brand button reused.

Show feedback on pointer-down. Commit the action on release through normal button semantics. Use a small fill change or scale near `0.98`; preserve visible keyboard focus. Loading must explain the operation without unexpectedly changing button width.

### Cards and data
Keep anatomy stable: label, value or content, units/timeframe, then comparison or supporting action. Use tabular numerals where alignment matters. Decoration is optional and must not encode essential information.

### Forms
Use persistent labels, clear boundaries, helper text near the field, and inline validation with recovery instructions. Do not rely on placeholders as labels. Errors use more than color, and disabled controls remain understandable.

### Charts
Provide readable axes, units, timeframes, and a text summary or accessible data alternative. Reserve luminous treatment for a focal series if it helps interpretation. Do not replace useful values with mystical symbols or label forecasts as facts.

### Feedback and empty states
Loading, empty, success, and error states share the same type and surface system. Explain what happened and what to do next. A small botanical illustration may soften an empty state; it cannot replace useful instructions.

## 9. Motion and Interaction

Motion explains a change. It is not an ambient performance.

- Respond immediately to input; do not add artificial delays.
- Use critically damped springs for panels and repositioning, with a response around **0.3–0.4 seconds**. Response describes behavior, not a fixed duration.
- Reserve mild overshoot for momentum-driven gestures, not ordinary menus or button presses.
- Animate from the current on-screen position and preserve velocity when retargeting. Never lock input merely because a transition is running.
- Enter and exit along the same path. Anchor popovers to their source.
- Animate primarily `transform` and `opacity`; avoid large continuously animated blur fields.
- Do not add drag interactions where a simple button serves the task better.

When dragging is useful, use Pointer Events and pointer capture, preserve the grab offset, track recent velocity, and hand that velocity to the settling spring. Select snap destinations using projected momentum, not just release position. Provide equivalent keyboard or button actions.

Routine work does not need confetti, sound, haptics, orbiting controls, or drifting particles. Delight comes from continuity and precision.

## 10. Accessibility and Resilience

Accessibility is part of the canonical aesthetic, not a separate reduced-quality theme.

- **Reduced motion:** replace travel, parallax, and springs with static changes or brief opacity feedback. Remove ambient loops and overshoot.
- **Reduced transparency:** use opaque surfaces and remove backdrop blur.
- **Increased contrast:** strengthen edges, text, and focus indicators; remove texture where it competes with content.
- **Keyboard:** use semantic controls, logical order, visible focus, and equivalent actions for gestures.
- **Dialogs:** contain focus, support Escape when appropriate, and restore focus to the trigger.
- **Touch:** aim for at least **44 × 44 CSS pixels** for primary touch controls, including their hit area.
- **Text and layout:** test enlarged text, long labels, translations, narrow screens, and landscape orientation.
- **Performance:** avoid layout shifts, optimize SVGs, limit blur, and ensure font loading does not disrupt the page.

Dark mode is the canonical expression. If a project needs light mode, preserve hierarchy and semantic roles while developing a separately tested light palette; do not mechanically invert the dark colors.

## 11. Starter Tokens

These tokens establish the visual foundation. Interactive state, semantic status, chart, and required-boundary colors must be added and contrast-tested for the project.

```css
:root {
  color-scheme: dark;

  --gb-canvas: #0d0d0d;
  --gb-surface: #121212;
  --gb-surface-raised: #1a1a1a;
  --gb-text: #e0e0e0;
  --gb-text-secondary: #b3b3b3;
  --gb-edge-decorative: #333333;

  --gb-brand: #b8506b;
  --gb-action: #ef6461;
  --gb-highlight: #d4af37;
  --gb-botanical: #5e8c61;

  --gb-font-ui: system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  --gb-font-editorial: Georgia, "Times New Roman", serif;

  --gb-space-2: 0.5rem;
  --gb-space-4: 1rem;
  --gb-space-6: 1.5rem;
  --gb-space-8: 2rem;
  --gb-space-12: 3rem;

  --gb-radius-control: 0.625rem;
  --gb-radius-card: 1rem;
  --gb-radius-overlay: 1.25rem;
}
```

The editorial stack is a portable fallback, not a required brand font. Choose one refined serif when a project needs a stronger editorial voice.

## 12. Adaptation Rules for New Projects

### Preserve
- Ink-dark foundation and stable reading surfaces.
- Cherry identity, rose action hierarchy, rare gold detail.
- Rose/arch signature with sparse botanical ornament.
- Shared spacing, shape family, and typographic roles.
- Immediate feedback, familiar controls, and accessible behavior.

### Adapt
- Content density and page structure.
- Editorial serif and large illustration style.
- Which featured area receives the arch.
- Domain-specific data and semantic colors.
- Navigation form appropriate to device and task.

### Avoid
- A different accent color for every card or section.
- Gold borders around everything.
- Blackletter interface labels.
- Glass inside glass.
- Ornament behind data or over focus rings.
- Cosmic or mystical language replacing concrete information.
- Slow ambient movement as a substitute for polish.
- Different layouts and behaviors for each mood preset.

Optional accent presets may change limited decorative emphasis, but component anatomy, semantic colors, navigation, spacing, and behavior stay consistent. The canonical rose/cherry expression remains the reference.

## 13. Example Composition

A typical application opens on an ink-black canvas. A quiet floating sidebar carries a small rose mark and clear navigation. A restrained heading introduces the page, with a rose primary action nearby. Content sits in aligned, near-opaque cards. One featured area has an architectural arch and a tiny gold marker. Faint vines remain at the outer edge. The interface responds instantly and stays calm as work changes.

An editorial site uses the same foundation with a larger serif title, broader negative space, and one botanical illustration. A library uses compact result rows and clear filters. An editor gives the document space and keeps ornament outside the working area.

The identity survives because it lives in the system—not in the quantity of decoration.

## 14. Review Checklist

Before calling a project Gothic Bloom, verify:

- [ ] The primary task, location, and next action are clear without ornament.
- [ ] Color communicates consistent roles; semantic status is not confused with branding.
- [ ] Typography, spacing, radii, and component anatomy form one system.
- [ ] The rose and arch carry the identity without motif overload.
- [ ] Reading surfaces are stable; translucency has a functional purpose.
- [ ] Text, controls, charts, and focus indicators meet tested contrast requirements.
- [ ] Keyboard, touch, dialogs, and gesture alternatives work.
- [ ] Motion is immediate and interruptible, with preference-aware fallbacks.
- [ ] Layout survives narrow screens, long content, and enlarged text.
- [ ] Loading, empty, error, and success states are designed.
- [ ] Decorative assets do not intercept input, create noise, or cause layout shifts.

**Final test:** remove the roses, vines, halos, and arches. The product should still feel coherent and usable. Restore only the details that make it unmistakably Gothic Bloom.
