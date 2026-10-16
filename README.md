# lombyte-project.github.io

Website of [Lombyte](https://github.com/lombyte-project/lombyte), served at
https://lombyte-project.github.io/. Edit `site/`; the `pages` workflow builds it
with data from the game repository and deploys on every push here, after each
progress update of the game and on demand. Data URLs carry `?v=` with a hash of
the data, so a reload after a deploy always shows the new state.

`site/namingdesk.html` is the Naming Desk, served at `/namingdesk`. Its data
(`naming-data/`: source, names, symbol files) is built on each deploy by
`naming_data/gen.py` from the game's main branch and progress report.
`naming_data/refs.json.gz` (which code touches each data address) comes from the
retail programs, which no repository holds: rebuild it with
`naming_data/make_refs.py` on a machine that has them.
