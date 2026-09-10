---
name: FirstRun
description: A maintainer workspace for revision-bound setup evidence.
colors:
  canvas: "#f6f7f9"
  surface: "#ffffff"
  ink: "#202632"
  muted: "#636c7c"
  line: "#e0e4eb"
  line-strong: "#c6ceda"
  blue: "#244fc7"
  blue-hover: "#1c3fa3"
  blue-light: "#eef3ff"
  positive: "#23704c"
  negative: "#ad3339"
  warning: "#8c5f15"
typography:
  headline:
    fontFamily: "Segoe UI, -apple-system, BlinkMacSystemFont, Helvetica Neue, sans-serif"
    fontSize: "clamp(26px, 2.2vw, 32px)"
    fontWeight: 650
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  body:
    fontFamily: "Segoe UI, -apple-system, BlinkMacSystemFont, Helvetica Neue, sans-serif"
    fontSize: "14px"
    lineHeight: 1.55
  code:
    fontFamily: "Cascadia Code, SFMono-Regular, Consolas, monospace"
    fontSize: "12px"
rounded:
  control: "5px"
  ledger: "6px"
spacing:
  control-gap: "8px"
  page-inset: "40px"
  mobile-inset: "20px"
components:
  button-primary:
    backgroundColor: "{colors.blue}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
  button-primary-hover:
    backgroundColor: "{colors.blue-hover}"
    textColor: "{colors.surface}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "8px 14px"
---

# FirstRun design system

## Overview

The execution score: a quiet, compact workspace where the selected execution
stage and its exact evidence stay together. Cool daylight surfaces and graphite
text support prolonged reading. Blue marks navigation and actionable controls;
semantic status always has a written label.

This records the implemented system in `apps/web/src/app/globals.css`, not a new
concept proposal. The setup surface passed an independent desktop/mobile visual
review. Authenticated case surfaces still need a live-configured visual pass.

## Colors

Blue is the sole navigation/action accent. Positive, negative and warning colors
describe observed states; they are not decorative card themes. Canvas, surface
and line tones create hierarchy without darkening the entire interface.

## Typography

Use the local workhorse UI stack for headings, descriptions and controls. Use
monospace only for hashes, code, commands and exact identifiers. Metadata is
compact, but remains subordinate to readable headings and outcome labels.
Allow full digests to wrap; shortened hashes retain their complete accessible
title. No external font request is needed to build or render the application.

## Layout

Desktop uses a fixed 204px navigation rail, a 68px account bar and a flexible
content column. Content insets shrink at 1250px. The rail becomes icon-only below
950px and a horizontal navigation bar below 700px. Mobile uses a single content
column with 20px side insets. Evidence panels keep horizontal scrolling confined
to code/diff blocks; page-level horizontal scrolling is a defect.

Repository health and repair state occupy separate ledger cells. Case pages use
stage navigation, a selected evidence panel and a provenance column. On narrower
screens provenance moves below the evidence and stage navigation becomes a grid.

## Elevation & Depth

Flat by default: borders and slight surface changes establish grouping. Only the
primary button has a small structural shadow. Do not add large floating cards,
blurred backgrounds or ornamental gradients to evidence views.

## Shapes

Controls have restrained corners; ledgers and grouped panels share a slightly
softer radius. Small circles denote status or numbered sequence, never substitute
for a status label. Thin lines connect persisted event rows.

## Components

- Buttons: primary for the relevant operation, secondary for refresh and
  inspection. Disabled controls remain visible with explanatory context.
- Navigation: one selected repository destination; blue keyboard focus is distinct
  from selected styling. The skip link becomes visible on focus.
- Stage selector: keyboard-operable tabs using arrows, Home and End; changing the
  selection reveals existing evidence, not a fabricated progress animation.
- Status: a small colored dot plus readable text. Pending, stale, failed and
  cancelled are distinct from verified.
- Code and diff: raw text, bounded scroll areas, restrained added/removed lines.
  Do not render repository HTML or Markdown as executable browser content.
- Empty/setup: state the missing configuration and the next real operator action.
  It is a working state, not a sample dashboard.

## Do's and Don'ts

- Do keep the tested revision and proof provenance inspectable.
- Do preserve reduced-motion behavior and visible keyboard focus.
- Do separate default-branch health from repair evidence.
- Don't invent repository rows, metrics, progress or success labels.
- Don't add decorative imagery to operational evidence.
- Don't turn a proof panel green from HTTP readiness or a model response alone.
