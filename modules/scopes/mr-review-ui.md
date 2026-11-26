## Scope: UI merge requests are reviewed by looking at the screenshots
A merge request is a **UI change** when its diff adds or changes anything a person sees: an HTML/CSS/JS view, a template, a page, a component, or markup and styles embedded in code. For a UI change, on top of the review above:

- **Find the mockup and the screenshots.** The mockup is the file or directory the ticket, the MR description or your brief names, or the one in the repo for this screen. The screenshots are the committed PNGs the MR description lists, normally a `mockup-<width>x<height>.png` and `build-<width>x<height>.png` pair per viewport (1440x900 and 390x844 unless the ticket names others).
- **Open every image.** Read each PNG/JPG/WebP with your file-read tool (in Claude Code, `Read` on an image file shows you the image). Compare each mockup/build pair, and compare the diff's markup and CSS against the mockup's own markup and CSS.
- **Check themed controls, including open pickers.** Statically look for bare `select`/`input` elements without theme styles, and check textareas, checkboxes, radios, sliders and file inputs too. A native select's closed-state styling does not theme its raw OS open list: require a styled listbox or `appearance: base-select` with a themed picker, and `color-scheme` on dark pages. Check that adjacent controls in a row share height and vertical center.
- **BLOCK** when any of these holds, naming the file and what differs:
  - a structural mismatch between mockup and build: layout pattern (for example transcript rows shipped as chat bubbles), alignment, element order, or a missing or extra region (header, navigation, composer, footer, empty or error states), even when the MR description explains it;
  - an unexplained deviation: a visible difference beyond a pixel nit that the MR description does not name with a reason;
  - screenshots are missing: no build screenshot, no mockup screenshot, or a required viewport absent;
  - an unstyled native form control, including an unthemed open picker, or missing open-state screenshots at 390 px and 1440 px for changed pickers;
  - the change has no mockup to compare against;
  - you could not open an image (say which file and what the tool returned). Not seeing the images means you cannot review the UI, so it is a block, never a pass.
- **Explained minor deviations are not blockers.** Do not block a minor deviation (contrast, tap-target size, spacing forced by real data, a platform constraint, accessibility) that the MR description names with a valid reason; list it as a nit. Differences that come only from placeholder versus real data are nits too. Never block on a pixel nit.
- **A PASS on a UI MR must say you viewed the images**: list every image path you opened and state that each mockup/build pair matches structurally. A UI PASS without that list is not a valid review.
