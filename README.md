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
| `scheduler.n proc`, `.conda environment`, `.request memory`, `.request disk` | job submission parameters |
| `scheduler.strip oauth credentials` | strips pycWB's hardcoded `use_oauth_services = scitokens` submit-file requirement (see below) — **only** for test/local pools with no SciTokens credmon; leave unset for real deployments |
| `data.channels` | `channelNamesRaw` |
| `data.data files` | `frFiles` (a generated per-IFO frame-cache list file, written from whichever frame path(s) `asimov-gwdata` or similar provides — string or list, one frame path per line) |
| `data.segment length`, `.time before`, `.time after` | the `time_left`/`time_right` follow-up window around `event time` |
| `likelihood.minimum frequency`, `.maximum frequency` | `fLow`/`fHigh` |
| `reference ifo` | `refIFO` (defaults to the first interferometer) |

Everything else in the generated `user_parameters.yaml` — cWB's threshold
and regulator parameters (`bpp`, `subnet`, `netRHO`, `netCC`, `segLen`,
etc.) — is a **fixed, untuned default**, copied from pycWB's own example
configuration. See the comments in
[`asimov_pycwb/templates/user_parameters.yaml.liquid`](asimov_pycwb/templates/user_parameters.yaml.liquid)
for the full list.

### Escape hatch: pre-seeding a real config

The template above deliberately doesn't cover everything pycWB can do (for
example, injections into synthetic noise, or hand-tuned thresholds). If a
file named `<production name>.yaml` already exists in the event
repository's `analyses/` directory (asimov's own `general.calibration_directory`
config value, `analyses` by default) when `build_dag` runs, it's used
directly instead of being rendered from the template — the same "pre-seed
a real config" convention other asimov pipelines use for a `.ini` file via
`event.repository.find_prods()` (this plugin can't reuse that helper
directly, since it hardcodes a `.ini` extension). This is how this
plugin's own end-to-end test (see below) exercises a real pycWB config
that the template doesn't yet support.

## End-to-end test

[`.github/workflows/e2e.yml`](.github/workflows/e2e.yml) runs a real pycWB
analysis against a real HTCondor pool (in a `htcondor/mini` container,
using the same `etive-io/actions` reusable actions as the sibling
`asimov-pycbc`/`asimov-pesummary` plugins): a genuine `asimov apply` +
`asimov manage build submit`, building and submitting a real DAG, then
waiting for pycWB's real merge step to produce a real, readable
`catalog.parquet`.

The test production's config is pre-seeded (see above), using pycWB's
built-in synthetic Gaussian noise generation and a single sine-Gaussian
burst injection (`injection.noise` / `injection.parameters` — see
`pycwb.modules.job_segment.job_segment`), rather than real strain data, so
the whole run is self-contained and fast. This needed two things this
plugin didn't otherwise need to know about, both worth flagging clearly:

- **pycWB on PyPI (`pip install pycWB`, this plugin's declared dependency)
  cannot actually be installed in an ordinary CI environment.** It
  unconditionally tries to build a compiled `cwb-core` C++ extension
  against ROOT + healpix-cxx (confirmed directly: a plain `pip install
  pycWB` fails outright without them). pycWB's unreleased `main` branch has
  a pure-Python install path instead (`PYCWB_DISABLE_WAT=1` skips the C++
  wavelet extension — see `pycwb/setup.py` and
  `envs/Dockerfile.ci-native` upstream), which is what
  `.github/actions/setup-pycwb-env` uses to install a real, working pycWB
  with no ROOT/cwb-core at all. Switch this to a plain PyPI install once a
  release ships with that flag.
- **pycWB downloads a small (~54 MB), public, Git-LFS-hosted wavelet
  cross-talk catalog on first use** (`pycwb.modules.xtalk`, from
  `github.com/PycWB/xtalk-data`). This is unrelated to the ROOT/cwb-core
  extension above and needs no credentials, and it downloads and validates
  correctly in CI (confirmed by the workflow's own runs).
- **pycWB's `HTCondor.create()` unconditionally writes a SciTokens
  requirement (`use_oauth_services = scitokens`) into every node's submit
  file, with no config option to skip it** (confirmed directly against
  `pycwb.modules.condor.condor` — there is no parameter, environment
  variable, or config key that disables this). On a real IGWN pool with a
  working SciTokens credmon, that's exactly what real frame-data access
  needs; this minimal test pool has no credmon at all, so every node job
  held forever with `Job credentials are not available` until this
  plugin's `build_dag()` learned to strip those lines back out when
  `scheduler.strip oauth credentials` is set (see the metadata table
  above) — opt-in, and only meant for credmon-less test/local pools like
  this one.

This workflow is green: a real DAG is submitted to a real HTCondor pool,
the batch analysis job and merge node both run for real over synthetic
noise, and the resulting `catalog/catalog.parquet` is a real, readable
cWB trigger table (confirmed with real column names — `rho`, `net_cc`,
`hrss_H1`, `sky_error_regions`, etc. — not a stub). Getting there took a
few rounds of CI-driven fixes (see the PR history): a broken `pip
install`, missing pycWB injection-parameter fields, a `submit_dag()`
signature mismatch, a `pathlib.Path` htcondor2 rejected, and pycWB's
`prepare_job_runs()` leaving the process's working directory changed
after it returns. One thing the test deliberately does *not* assert:
whether the injected sine-Gaussian burst actually clears cWB's detection
thresholds (a question of amplitude tuning, not of the plugin's
correctness) — a real, non-empty merge is the completion criterion; a
nonzero trigger count is a bonus signal the workflow logs but doesn't
require.

## What's implemented

- `build_dag`: if a `<production name>.yaml` already exists in the event
  repository's `analyses/` directory, uses it as-is; otherwise renders
  `user_parameters.yaml` from the template above. Either way it then calls
  pycWB's `prepare_job_runs` + `HTCondor(...).create(..., submit=False)` to
  generate a real HTCondor DAGMan workflow (`condor/*.dag`) without
  submitting it.
- `submit_dag`: submits the pre-built DAG via asimov's configured
  `scheduler.submit_dag()`, and records the returned cluster ID as
  `production.job_id`.
- `detect_completion`: checks for `catalog/progress.parquet`, which pycWB's
  DAG `merge` node writes once the batch job has recorded real per-lag
  progress (`catalog/catalog.parquet` exists from much earlier — pycWB
  creates it, empty, while `build_dag` is still constructing the DAG — so
  it isn't a reliable completion signal on its own).
- `collect_assets`: returns the merged catalog, any per-trigger
  `skymap_statistics.json` files, and any unmerged per-job waveform
  reconstruction files (`output/wave_*.h5`).

## Known limitations / TODOs

This is a first-pass scaffold. `build_dag`/`submit_dag`/`detect_completion`
are now verified against a real pycWB run on a real HTCondor pool (see
"End-to-end test" above); the rest of this list is still unverified against
a real run:

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
- **`collect_assets` is best-effort.** It's based on reading pycWB's
  merge/output code, but unlike `detect_completion` (verified against a
  real merge via `catalog/progress.parquet`), the e2e test doesn't exercise
  its skymap/waveform paths (the tiny test config doesn't reliably
  guarantee a detected trigger, and its DAG doesn't merge waveforms at all
  — see below) — those may still need adjusting.
- **No GraceDB upload integration.** pycWB has `pycwb.modules.gracedb` for
  this; it isn't wired up here.
- **Re-running `build_dag` is destructive.** pycWB's `HTCondor.create()`
  interactively confirms before touching an existing `condor/` directory,
  which would hang a non-interactive asimov run, and pycWB's own
  `prepare_job_runs(..., overwrite=True)` is a resume feature that reuses
  any existing catalog/progress/trigger/output rather than starting fresh
  — which would otherwise let `detect_completion()` see stale state from a
  previous run. So `build_dag()` removes `condor/`, `catalog/`, `trigger/`,
  `output/`, `job_status/`, and `log/` itself before regenerating the DAG
  (leaving the downloaded `wdmXTalk/` catalog in place). This means calling
  `build_dag()` again after a production has already been submitted (or
  has run) will discard its existing results.

## Development

```
pip install -e .[test]
pytest
```

The unit test suite only covers config-template rendering and the
pre-seeded-config lookup (`PyCWB._render_config`/`_find_existing_config`,
via `asimov.pipeline.Pipeline`), since that only depends on `asimov` and
`liquidpy`. `build_dag`/`submit_dag`'s pycWB-calling code isn't unit
tested — it's covered instead by the end-to-end workflow described above,
which runs a real pycWB installation against a real HTCondor pool.

### Installing pycWB

`pip install asimov-pycwb` does **not** pull in pycWB itself: `pip install
pycWB` unconditionally tries to build a compiled `cwb-core` C++ extension
against ROOT + healpix-cxx, which fails outright without them (there's no
manylinux wheel), so making it an unconditional dependency would break
installation for anyone without a ROOT-enabled environment already. Install
pycWB separately, using whichever of these fits your environment:

- **A ROOT-enabled conda environment** (see
  [pycWB's own README](https://github.com/PycWB/pycwb#installation) for the
  `conda install ... root=6 healpix_cxx=3 ...` recipe), then
  `pip install asimov-pycwb[pycwb]` to also record the dependency; or
- **pycWB's pure-Python install path** (no ROOT/cwb-core at all):
  `PYCWB_DISABLE_WAT=1 pip install "pycwb @ git+https://github.com/PycWB/pycwb.git"`
  — this is what `.github/actions/setup-pycwb-env` uses for this plugin's
  own end-to-end test, since that flag isn't in any released PyPI version
  yet (see `pycwb/setup.py` upstream).
