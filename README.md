# asimov-pycwb

An [asimov](https://pypi.org/project/asimov/) pipeline plugin wrapping
[pycWB](https://github.com/PycWB/pycwb) — a Python implementation of the
Coherent WaveBurst (cWB) gravitational-wave burst analysis pipeline.

## Design decision: targeted follow-up, not the all-sky search

asimov orchestrates *per-candidate* analysis jobs: for each `Production`
(one analysis of one event) it asks a `Pipeline` subclass to build a
config, submit an HTCondor job/DAG, detect completion, and collect result
assets. This is the same abstraction used for parameter-estimation
pipelines such as bilby, RIFT, or pyRing.

cWB/pycWB, however, is primarily a **continuous, all-sky search** pipeline:
it scans a long stretch of data across all times and directions looking for
triggers. That mode has no per-candidate structure at all, and doesn't map
onto asimov's data model — there's no single "Production" it corresponds
to.

pycWB also supports a **targeted/single-candidate reconstruction mode**,
however: a short segment of data centred on a *known* trigger time, run
through the same search-configuration machinery, to produce a sky
localisation and waveform reconstruction for that one candidate. This is
the mode used for burst follow-up of a known event (see
[`examples/GW190521_search/user_parameters.yaml`](https://github.com/PycWB/pycwb/blob/main/examples/GW190521_search/user_parameters.yaml)
in the pycWB repository, and pycWB's `gps_center`/`time_left`/`time_right`
config keys in `pycwb.modules.job_segment.job_segment`).

**This plugin targets that single-candidate mode only.** It is a
deliberate scope decision, not an oversight: a full all-sky search doesn't
fit asimov's per-candidate `Production` model, so there is no sensible way
for this plugin to expose it. If you want to run pycWB's continuous
search, use pycWB's own CLI (`pycwb run` / `pycwb batch-runner`) directly,
outside of asimov.

## Usage

Add an analysis to an event using a blueprint like:

```yaml
kind: analysis
pipeline: pycwb
name: pycwb-followup
comment: A pycWB targeted single-candidate reconstruction
```

```
$ asimov apply -f pycwb-followup.yaml -e GW150914_095045
```

The plugin reads the following keys from the production's metadata
(`production.meta`, the same dictionary populated by asimov blueprints and
data-fetching pipelines such as `asimov-gwdata`):

| Key | Used for |
| --- | --- |
| `interferometers` | `ifo` list |
| `event time` | `gps_center` (the trigger time to follow up) |
| `scheduler.accounting group` | HTCondor `accounting_group` (**required**) |
| `scheduler.n proc`, `.conda environment`, `.memory`, `.disk` | job submission parameters |
| `data.channels` | `channelNamesRaw` |
| `data.data files` | `frFiles` (per-IFO frame-cache list files, e.g. from `asimov-gwdata`) |
| `data.segment length`, `.time before`, `.time after` | the `time_left`/`time_right` follow-up window around `event time` |
| `likelihood.minimum frequency`, `.maximum frequency` | `fLow`/`fHigh` |
| `reference ifo` | `refIFO` (defaults to the first interferometer) |

Everything else in the generated `user_parameters.yaml` — cWB's threshold
and regulator parameters (`bpp`, `subnet`, `netRHO`, `netCC`, `segLen`,
etc.) — is a **fixed, untuned default**, copied from pycWB's own example
configuration. See the comments in
[`asimov_pycwb/templates/user_parameters.yaml.liquid`](asimov_pycwb/templates/user_parameters.yaml.liquid)
for the full list.

## What's implemented

- `build_dag`: renders `user_parameters.yaml` from the template above, then
  calls pycWB's `prepare_job_runs` + `HTCondor(...).create(..., submit=False)`
  to generate a real HTCondor DAGMan workflow (`condor/*.dag`) without
  submitting it.
- `submit_dag`: submits the pre-built DAG via asimov's configured
  `scheduler.submit_dag()`, and records the returned cluster ID as
  `production.job_id`.
- `detect_completion`: checks for the merged `catalog/catalog.parquet` file
  that pycWB's DAG `merge` node produces once all batch jobs have finished.
- `collect_assets`: returns the merged catalog, any per-trigger
  `skymap_statistics.json` files, and any unmerged per-job waveform
  reconstruction files (`output/wave_*.h5`).

## Known limitations / TODOs

This is a first-pass scaffold, written by reading pycWB's and asimov's
source rather than by testing against a real run — treat all of the
following as unverified until exercised end-to-end:

- **Only run/segment/IFO/data fields are templated.** cWB's analysis
  thresholds and regulators are fixed defaults; there's no blueprint-level
  way to tune them yet.
- **Data-quality/veto files (`DQF`) are not templated at all** — the
  generated config always sets `DQF: []`. Asimov's veto-file metadata
  conventions vary across pipelines, and this needs a deliberate design
  decision rather than a guess.
- **No FITS skymap.** pycWB writes sky-localisation output as a per-trigger
  `skymap_statistics.json` file, not a FITS file. Asimov's base
  `Pipeline.store_results()` expects a `{production.name}_skymap.fits`
  file — a natural fit for cWB's sky map, in principle — but converting
  the JSON output into an actual FITS skymap (e.g. via `ligo.skymap` or
  `healpy`) is not implemented.
- **Waveform reconstruction files aren't merged.** The DAG built by
  `build_dag` only runs `pycwb merge` for the catalog and progress files
  (matching what pycWB's own `condor.py` generates); per-job
  `output/wave_*.h5` files are left unmerged. Merging them would need an
  extra `pycwb merge --wave` DAG node or an `after_completion()` hook.
- **`detect_completion`/`collect_assets` are best-effort.** They're based
  on reading pycWB's merge/output code, not on watching a real run
  complete — the exact paths may need adjusting.
- **No GraceDB upload integration.** pycWB has `pycwb.modules.gracedb` for
  this; it isn't wired up here.
- **Re-running `build_dag` is destructive.** pycWB's `HTCondor.create()`
  interactively confirms before touching an existing `condor/` directory,
  which would hang a non-interactive asimov run — so `build_dag()` removes
  that directory itself before regenerating the DAG. This means calling
  `build_dag()` again after a production has already been submitted (or
  has run) will discard its existing DAG.

## Development

```
pip install -e .[test]
pytest
```

The test suite only covers config-template rendering (`PyCWB._render_config`,
via `asimov.pipeline.Pipeline`), since that only depends on `asimov` and
`liquidpy`. It does not exercise `build_dag`/`submit_dag` against a real
HTCondor pool or a full pycWB installation (which requires pycWB's compiled
`cwb-core`/ROOT dependencies) — that's left as follow-up work once this
plugin is tried against a real production.
