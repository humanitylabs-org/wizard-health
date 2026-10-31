# Source and license notice

Health started from Wizard App Manager's hello-wizard reference template (GPL-3.0-only) and keeps its canonical Wizard style snapshot (`web/boot.js`, `web/wizard-tokens.css`, `web/wizard-ui.css`, `web/wizard-sheet.js`, `web/standalone.css`, pinned by `appearance-provenance.json`) unchanged. The view loads the shared `/wizard-os-home/core/app-client.js`. The server is Python standard library only. The dashboard drawing (recovery ring, stage bar, trend charts) moved here from the WHOOP connector app (oscarhenrycollins/wizard-whoop, same authors and license). WHOOP is a trademark of WHOOP, Inc.; Health is not affiliated with any wearable maker. Distributed under GPL-3.0-only; no warranty. This repository contains no health data or credentials.

## Health 0.3 looks (motion and drawing)

All written for this app, dependency-free, with no vendored code and no CDN. Ideas taken from libraries in Teknium's Uncommon Web field guide (https://webdev.teknium.io/):

- Rolling digits (`web/motion.js`, `flow()`): a small CSS odometer after NumberFlow (https://github.com/barvian/number-flow, MIT). No NumberFlow code is included.
- The living orb (`web/motion.js`, `Blobs`): a 2D-canvas blob drawn from seven wandering points on a closed Catmull-Rom curve, after the blobs library (https://github.com/g-harel/blobs, MIT) and Paper Shaders' mesh-gradient and metaball looks (https://github.com/paper-design/shaders, PolyForm Shield / see upstream). No code from either is included.
- Swipe cards (`web/looks.js`): a stacked deck with a small spring integrator, after SmoothUI's swipe stacks and Swiper's cards effect (https://smoothui.dev, MIT; https://swiperjs.com, MIT). No code from either is included.
- The year heatmap (`web/looks.js`): 4x4 Bayer ordered dithering drawn on a canvas, after dithercn (shadcn-style dither components). No code from it is included.
