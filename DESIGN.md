---
name: AXIS Redwood
description: A warm, compact conversation workspace for local browser tasks.
colors:
  canvas: '#fbf9f8'
  surface: '#fff'
  shell: '#312d2a'
  shell-text: '#fbf9f8'
  shell-hover: '#514c47'
  shell-ready: '#bdd9ae'
  shell-warning: '#ffd18c'
  text: '#312d2a'
  muted: '#6f6964'
  border: '#d4cfca'
  soft: '#f1efed'
  accent: '#bd4332'
  accent-hover: '#9b382a'
  on-accent: '#fff'
  accent-soft: '#ffebe8'
  green: '#436b35'
  danger: '#a12532'
  warning: '#805c10'
  focus: '#bd4332'
  overlay: rgb(32 30 28 / 0.42)
  dark-canvas: '#201e1c'
  dark-surface: '#312d2a'
  dark-shell: '#161513'
  dark-text: '#f5f4f2'
  dark-muted: '#bcb6b1'
  dark-border: '#665f5b'
  dark-soft: '#3a3632'
  dark-accent: '#eea59c'
  dark-accent-hover: '#f7bfb8'
  dark-on-accent: '#38140f'
  dark-accent-soft: '#491b14'
  dark-green: '#bdd9ae'
  dark-danger: '#ffb3bc'
  dark-warning: '#e8c46f'
  dark-focus: '#eea59c'
typography:
  headline:
    fontFamily: '"Oracle Sans", "Segoe UI", system-ui, sans-serif'
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.4
  title:
    fontFamily: '"Oracle Sans", "Segoe UI", system-ui, sans-serif'
    fontSize: 16px
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: '"Oracle Sans", "Segoe UI", system-ui, sans-serif'
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  answer:
    fontFamily: '"Oracle Sans", "Segoe UI", system-ui, sans-serif'
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: '"Oracle Sans", "Segoe UI", system-ui, sans-serif'
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.5
  status:
    fontFamily: '"Oracle Sans", "Segoe UI", system-ui, sans-serif'
    fontSize: 12px
    fontWeight: 600
    lineHeight: 1.5
rounded:
  control: 6px
  surface: 12px
  item: 4px
spacing:
  '4': 4px
  '6': 6px
  '8': 8px
  '12': 12px
  '16': 16px
  '20': 20px
  '24': 24px
  '32': 32px
components:
  button-primary:
    backgroundColor: '{colors.accent}'
    textColor: '{colors.on-accent}'
    rounded: '{rounded.control}'
    padding: 6px 12px
  button-primary-hover:
    backgroundColor: '{colors.accent-hover}'
  button-secondary:
    textColor: '{colors.text}'
    rounded: '{rounded.control}'
    padding: 6px 12px
  button-secondary-hover:
    backgroundColor: '{colors.soft}'
  button-icon:
    rounded: '{rounded.control}'
    padding: 8px
    width: 36px
    height: 36px
  button-send:
    backgroundColor: '{colors.accent}'
    textColor: '{colors.on-accent}'
    rounded: '{rounded.control}'
    padding: 10px
    width: 40px
    height: 40px
  input-text:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.text}'
    rounded: '{rounded.control}'
    padding: 8px 10px
  workspace-navigation:
    backgroundColor: '{colors.shell}'
    textColor: '{colors.shell-text}'
    padding: 0 16px
  context-chip:
    backgroundColor: '{colors.soft}'
    textColor: '{colors.muted}'
    typography: '{typography.label}'
    rounded: '{rounded.control}'
    padding: 0 6px 0 8px
  permission-card:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.text}'
    rounded: '{rounded.surface}'
    padding: 12px
  composer:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.text}'
    rounded: '{rounded.surface}'
    padding: 10px 12px
  history-row:
    rounded: '{rounded.control}'
    padding: 12px 8px
  history-row-selected:
    backgroundColor: '{colors.accent-soft}'
  task-badge:
    textColor: '{colors.muted}'
    typography: '{typography.status}'
  task-badge-active:
    textColor: '{colors.green}'
---

# Design System: AXIS Redwood

## Overview

**Creative North Star: "The Redwood Conversation Workspace"**

AXIS uses the user-selected Oracle Redwood direction: warm neutral surfaces, a charcoal shell, and restrained red actions. The existing AXIS mark retains its geometry. The interface is compact and rectangular, with local sans typography and a consistent authored SVG stroke family.

Flat surfaces, readable text, and explicit state carry the interface. Shared controls and review patterns connect the side panel, full-tab settings, and approval popup. Capabilities remain available through progressive disclosure; visual simplification does not change task ownership, execution modes, or approval semantics.

**Key Characteristics:**

- Warm light surfaces and an explicit charcoal dark theme.
- Local sans typography with stronger leading for answers.
- Small control corners, larger contained-surface corners, and restrained borders.
- Authored SVG icons, labeled states, and visible keyboard focus.
- Short functional motion that respects reduced-motion preferences.

This records the finished vanilla JavaScript/CSS extension, not a seed or a complete Oracle Redwood library. Tokens come from [sidepanel.css](browser-agent-bridge/extension/sidepanel.css). Behavior is evidenced by [sidepanel.js](browser-agent-bridge/extension/sidepanel.js) and the shared [UI modules](browser-agent-bridge/extension/ui). [PRODUCT.md](PRODUCT.md) owns product requirements; the [surface contract](.impeccable/surfaces/browser-agent-bridge-extension-sidepanel-html.md) owns the selected composition.

## Colors

The palette combines warm paper, charcoal, and restrained red. Frontmatter names match CSS custom-property names without their leading dashes. Dark-prefixed entries record actual dark-theme overrides. Component entries describe the default palette; runtime semantic variables resolve the active theme.

### Primary

- **Redwood red: accent:** primary actions, Send, links, and selected theme text. Accent-hover supplies hover treatment; on-accent supplies the foreground.
- **Soft red: accent-soft:** selected history rows and the return-to-active-task strip.
- **Focus red: focus:** keyboard and composer focus. The toolbar instead uses shell-warning for its focus outline.
- **Dark-theme coral:** dark-accent, dark-accent-hover, dark-on-accent, and dark-accent-soft provide the dark action treatment.

### Neutral

- **Warm paper: canvas:** application ground.
- **Surface:** input, composer, review, menu, and dialog surfaces.
- **Charcoal: shell and text:** toolbar ground and default foreground, with distinct semantic names despite their shared light-theme value.
- **Shell-text and shell-hover:** toolbar content and hover treatment.
- **Muted:** secondary copy, metadata, field labels, and inactive status.
- **Border and soft:** dividers and strokes; low-emphasis fills such as user messages, attachments, and code.
- **Overlay:** translucent modal backdrops.

### Semantic states

Green identifies active/completed status and positive feedback. Warning identifies paused, interrupted, limit-reached, and needs-user states. Danger identifies failures, destructive menu text, and errors. Shell-ready and shell-warning are connection-indicator colors on the charcoal toolbar.

**The Semantic State Rule.** Brand red identifies actions and emphasis; success, warning, and danger retain their separate semantic roles. Status is accompanied by text.

Dark mode overrides the documented dark-prefixed values and shadow. Shell-text, shell-hover, shell-ready, shell-warning, and overlay retain their base values. Explicit Light overrides the operating-system preference; System follows it. The unused mint declaration is not promoted into the palette. The source defines no tonal ramp, so the sidecar does not invent one.

## Typography

**Body font:** locally available Oracle Sans, then Segoe UI, system-ui, and sans-serif. The extension does not bundle or fetch a webfont. Headings use the same stack; there is no separate display face. Technical code retains browser monospace behavior without a newly prescribed font family.

The compact ramp supports narrow panels. Weight distinguishes headings; answer prose gains room through leading.

### Hierarchy

- **Headline:** empty-state and history titles.
- **Title:** conversation headings, settings groups, review headings, and answer h2/h3 subheadings.
- **Body:** default application copy.
- **Answer:** response prose, with a maximum line length of 72ch.
- **Label:** metadata, settings descriptions, menu items, and compact controls. The composer hint has an observed tighter line-height of 1.4.
- **Status:** badges and compact phase labels. Counters and timestamps use tabular numerals where defined.

Sizes, weights, and normal leading are normative in the frontmatter. The brand wordmark has letter-spacing of 0.02em; ordinary interface copy retains natural casing and spacing.

**The Local Type Rule.** Use the packaged local font stack. Do not add a remote font or a new display face to this product.

## Layout

The side panel is a full-height flex column using dynamic viewport units. Its toolbar has a 52px minimum height; the timeline takes remaining space and scrolls independently. The selected composition places one live task strip above the composer. New activity participates in normal flow immediately before the composer, keeping it clear of wrapped controls.

Typical horizontal gutters are 16px and timeline entries are separated by 20px. User messages align right at no more than 90% width; assistant answers align left. The composer textarea grows from a 52px minimum to a 160px maximum.

History is a full-height modal drawer with a 440px maximum width. The task-switch dialog caps at the smaller of 420px and 90vw. Full-tab settings center within 720px with 24px side gutters. Attachments use an adaptive grid with a 170px preferred minimum constrained by available width.

### Responsive behavior

- At widths of 360px and below, main panel gutters narrow to 12px, the composer tightens, and settings rows can wrap.
- At widths of 280px and below, the brand wordmark hides, task controls wrap below their copy, composer controls reflow, and history timestamps hide.
- At heights of 640px and below, the textarea minimum becomes 44px and task/settings spacing tightens.
- At heights of 480px and below, the workspace can scroll and composer/approval-stack height caps relax.
- Normally, the composer and approval stack each cap at 48dvh. Approval review content caps at 24dvh in-panel and 50dvh in the popup. Execution review is keyboard-focusable and explicitly signals that more detail can be scrolled.

These are observed rules, not claims that every combined viewport has been tested.

## Elevation & Depth

The system is predominantly flat. Surface tone and thin borders separate ordinary content. Floating action menus use the shared soft shadow. History and task-switch dialogs separate from the application through a translucent backdrop.

### Shadow vocabulary

- **Menu shadow:** the light-theme shadow custom property.
- **Dark menu shadow:** the dark-theme shadow override.

Exact shadows and motion values are recorded in the sidecar. Focus and other component treatments are retained in its source-derived snippets.

**The Flat Surface Rule.** Ordinary content, controls, and cards remain flat. Floating action menus use the shared shadow; modal separation uses the shared backdrop.

With reduced motion not requested, buttons transition background and border color over 160ms with ease-out. History enters from a 16px left offset over 180ms with cubic-bezier(0.16, 1, 0.3, 1). These effects are gated by prefers-reduced-motion: no-preference.

## Shapes

Controls, inputs, chips, and menus use the control radius. Composer, user messages, approval cards, and task-switch dialogs use the surface radius. Menu items and appearance segments use the smaller item radius. Thin borders define controls and review containers; the drawer and toolbar keep straight outer edges.

The AXIS mark remains the local icon.svg at 24px. Interface SVGs use a 24-unit viewBox, a 1.7-unit current-color stroke, and rounded caps/joins. Most render at 20px, with 16px variants in compact contexts. Preserve the existing geometry and SVG authoring approach.

## Components

### Buttons

Default buttons have a 36px minimum height, control-radius corners, a thin border, and compact padding. Primary buttons use accent/on-accent; neutral buttons are transparent. Neutral hover uses soft and active uses border, subject to the more specific primary-hover rule. Disabled controls reduce opacity to 0.55.

Icon buttons have a 36px square box and an accessible name. Send is a 40px square primary control. The connection indicator and some composer icons are narrower; 36px is not a universal target width.

Focus-visible normally uses a 2px focus-color outline with a 3px offset. Toolbar controls use shell-warning. The composer supplies a single enclosing focus treatment.

### Inputs / Fields

Inputs and selects use surface, border, control-radius corners, and 8px by 10px padding. Placeholder copy uses muted; carets use accent. Checkbox/radio inputs use accent and an 18px box.

The composer contains a transparent textarea and a wrapping control row. Its focus-within state colors the border and adds a 1px outline at zero offset; the textarea suppresses a second focus outline. Attachment, execution mode, target, task menu, and Send remain available.

### Chips and status

Context chips use soft with muted text and a compact remove control. Attachment tiles use the same restrained treatment. Status badges pair text with a small outlined dot; active/completed dots fill. Their labels carry meaning alongside color.

### Cards / Containers

User messages use a soft-filled bubble. Approval cards use surface, a thin border, surface-radius corners, and compact padding. Task activity uses dividers and disclosures rather than elevated cards.

Execution approvals lead with Action, Target, Destination, and the supplied proposed change. Remaining fields stay in a scrollable region with a visible continuation hint; exact technical data remains available in a disclosure. Bridge approvals share styling and retain existing decision semantics.

### Navigation and history

A charcoal toolbar holds the AXIS mark, connection status, and named icon controls. History uses a native modal dialog with search, date groups, selected-row tint, and per-conversation actions. Rename and deletion confirmation appear inline. Task ownership changes use a separate native modal task-switch dialog.

Action menus are native auto popovers with menu semantics, not dialogs. Arrow Up/Down, Home/End, Escape, and Tab are handled; action and keyboard dismissal restore trigger focus. Native outside dismissal closes the popover, and resizing closes it. Popovers can appear within the history dialog without its scrolling region clipping them.

### Settings and progressive disclosure

Connection and Appearance precede expandable Advanced settings. Theme is a native radio group styled as a segmented control; checked segments use surface, accent text, and stronger weight. Changes apply immediately, save locally, and restore the previous selection if saving fails.

Current work has one live Pause/Resume and Stop control set in the task strip. Timeline activity starts collapsed. Streaming reconciliation is implemented to retain focus and reading position. These descriptions record shared UI behavior rather than new backend requirements.

## Do's and Don'ts

### Do:

- **Do** use semantic CSS variables so light, dark, and system appearance remain consistent across extension surfaces.
- **Do** preserve the AXIS mark geometry and use the authored SVG family for interface icons.
- **Do** retain visible keyboard focus, accessible names for icon controls, and text labels for status.
- **Do** let long content wrap and expose scrollable approval details with a visible continuation cue.
- **Do** keep native popover menus distinct from modal history and task-switch dialogs.
- **Do** place live task controls in the single current-task strip and keep answers readable above collapsed activity.

### Don't:

- **Don't** add remote fonts, runtime design dependencies, ornamental gradients, or decorative animation.
- **Don't** replace semantic success, warning, or danger colors with brand red.
- **Don't** duplicate live Pause/Resume and Stop controls across the timeline and composer.
- **Don't** truncate decision-critical approval values or substitute a generated summary for supplied data.
- **Don't** convert the restrained control geometry into pill-shaped navigation or add shadows to ordinary cards.

Evidence includes the source above, the [UI verification report](tmp/axis-redwood-qa/verification.json), [native verification report](tmp/axis-redwood-native/native-verification.json), and [finish verdict](.impeccable/review/finish-verdict.md). Those reports establish their named checks only; this documentation pass did not run a new browser review. The verdict resolved the three scored finish findings. Unused declarations and PRODUCT.md's missing schema stamp are not repaired or canonized by this record.
