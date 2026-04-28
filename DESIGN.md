# AI Panel Design System

## Direction

AI Panel is a local comparison workspace for reading multiple model answers side by side. It should feel like a focused developer documentation tool, not a terminal log viewer or marketing page.

The design combines:

- Mintlify: reading-first developer documentation clarity.
- Notion: warm neutral canvas, quiet borders, topic-centered history.
- Claude: warm AI-product tone and model comparison cards.

## Visual Rules

- Use a warm off-white page background, not pure gray or black.
- Use white or ivory panels with thin warm borders.
- Avoid heavy shadows, gradients, decorative blobs, and dark code-like answer blocks.
- Keep radius restrained: 6px for controls, 8px for panels/cards.
- Make the three model answers the primary visual object.
- Treat AI answers as documents, not code. Use readable sans-serif text with relaxed line height.

## Color Tokens

- Background: `#f8f7f3`
- Panel: `#fffdfa`
- Raised Surface: `#ffffff`
- Border: `#e5e1d8`
- Border Strong: `#d7d1c5`
- Text: `#191816`
- Muted Text: `#6f6a62`
- Faint Text: `#9a9489`
- Primary Accent: `#c96442`
- Primary Accent Hover: `#ad5437`
- Success: `#0f8f61`
- Warning: `#a86413`
- Error: `#b42318`
- Soft Warning Surface: `#fff7ed`

## Typography

- UI and body text: system sans-serif.
- Do not use viewport-based font sizing.
- Letter spacing is `0`.
- Section headings are compact and functional.
- AI answer bodies use `14px` to `15px`, `1.65` line height, and normal sans-serif text.
- Monospace is reserved for paths, command snippets, and raw JSON only.

## Layout

- Left sidebar: topic input, mode selection, run button, CLI connection, recent results.
- Right workspace: selected run header, failure recovery, tabs, summary, model comparison grid.
- Default result tab is `전체 비교`.
- In `전체 비교`, show Claude, Gemini, and Codex in equal-width columns on desktop.
- Stack model cards on narrower screens.
- Recent results are labeled by topic first, run id second.

## Components

- Primary button: terracotta background, white text, 6px radius.
- Secondary buttons: ivory/white background, warm border, near-black text.
- Agent rows: compact two-column rows with connection action.
- Failure panel: warm warning surface with direct recovery buttons.
- Model card: white document surface, thin border, 8px radius, no dark background.
- Answer body: scrollable document region with warm white background and subtle top border.

## Do Not

- Do not use dark `pre` blocks for normal model answers.
- Do not make the UI look like a landing page.
- Do not add decorative illustrations, orbs, gradients, or oversized hero text.
- Do not hide failure recovery behind raw logs.
- Do not let long answer text resize the overall layout unexpectedly.
