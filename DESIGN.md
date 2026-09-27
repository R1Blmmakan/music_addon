# DESIGN.md - BitChord Homelab Relay

## Product & Brand Identity
- **Product**: BitChord Lossless Addon (Self-hosted Homelab Audio Relay)
- **Audience**: Audiophiles, self-hosters, and BitChord Android client users
- **Metaphor**: Studio Audio Hardware Rack / Precision Mastering Console
- **Philosophy**: Functional hardware authenticity. Clean matte surfaces, precision instrumentation, zero decorative AI slop.

## Dials (Antislop Core Part 3)
- **ENERGY**: 2 (Balanced, professional studio instrumentation)
- **RHYTHM**: 2 (Structured rack hierarchy with distinct functional bays)
- **MOTION**: 1 (Immediate, functional hover and focus transitions; zero endless looping animations)

## Palette (R-29: 2 Core + 1 Accent)
- **Chassis Background**: `#0d0e12` (deep matte carbon)
- **Panel Surface**: `#15171e` (studio rack slate)
- **Panel Elevated**: `#1b1e28` (interactive bays and inputs)
- **Borders & Bezels**: `#282c3a` (machined steel dividers)
- **Text Primary**: `#f3f4f6` (bone white, contrast > 13:1)
- **Text Muted**: `#9ca3af` (neutral grey, contrast > 6:1)
- **Signal Active (LED)**: `#10b981` (emerald green)
- **Signal Standby (LED)**: `#f59e0b` (studio amber)
- **Interactive Accent**: `#38bdf8` (cyan indicator for links & actions)

## Typography (R-06)
- **Primary Headings & Body**: `Plus Jakarta Sans`, sans-serif (clean, humanist geometric grotesk)
- **Instrumentation & Code**: `JetBrains Mono`, monospace (sample rates, bit depths, endpoints, JSON keys)

## Rules & Gates
- No em dashes in copy (R-02)
- No glowing or pulsing indicator dots (R-13, R-19)
- No eyebrow pill badges above headlines (R-09)
- No colored decorative left stripes (R-01)
- No fake metrics or claims (R-17, R-36)
- All interactive controls functional with clear feedback (R-26)
- Client-side token storage only: never leak or send server tokens over public HTML
