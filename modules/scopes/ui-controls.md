## When the change touches UI

- Port the ticket's or repo's committed mockup markup/CSS; do not re-interpret it. For a new/reworked screen without a mockup, report the gap in your summary and any MR; never invent a layout.
- Theme every form control, never leave OS defaults: selects/dropdowns (including open pickers), text inputs, textareas, checkboxes, radios, sliders and file inputs. A native select opens as a raw OS list: use a styled listbox or `appearance: base-select` with a themed picker. Set `color-scheme` on dark pages. Adjacent controls in a row share height and vertical center.
- Where a browser is available (the code-ui row), screenshot at 390 px and 1440 px, including pickers open, and commit the screenshots next to the mockup ones. This does not grant browser tools; report missing screenshots in your summary and any MR when no browser is available.
