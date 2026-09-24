"""
Unit tests for the pycWB config template.

These only exercise config rendering (``PyCWB._render_config``), which needs
``asimov`` and ``liquidpy`` but not pycWB itself - pycWB is only imported
lazily inside ``build_dag``, since it depends on compiled cwb-core/ROOT
extensions that aren't required just to check the template renders sensible
YAML from a production's metadata.
"""

import yaml

from asimov_pycwb.pipeline import PyCWB


class FakeRepository:
    def __init__(self, directory):
        self.directory = str(directory)


class FakeEvent:
    def __init__(self, name, repository=None):
        self.name = name
        self.repository = repository


class FakeProduction:
    """A minimal stand-in for asimov.analysis.Analysis, just enough for
    asimov.pipeline.Pipeline.__init__ and PyCWB._render_config to run."""

    def __init__(self, rundir, meta, repository=None):
        self.event = FakeEvent("GW150914_095045", repository=repository)
        self.name = "pycwb-test"
        self.category = None
        self.rundir = str(rundir)
        self.meta = meta


def make_meta(**overrides):
    meta = {
        "interferometers": ["H1", "L1"],
        "event time": 1126259462.4,
        "scheduler": {"accounting group": "ligo.dev.o4.burst.allsky.cwboffline"},
        "data": {
            "channels": {"H1": "GDS-CALIB_STRAIN", "L1": "GDS-CALIB_STRAIN"},
        },
    }
    meta.update(overrides)
    return meta


def test_template_renders_valid_yaml(tmp_path):
    production = FakeProduction(tmp_path, make_meta())
    pipeline = PyCWB(production)

    config_file = pipeline._render_config()

    with open(config_file) as f:
        rendered = f.read()
        f.seek(0)
        config = yaml.safe_load(f)

    assert config["ifo"] == ["H1", "L1"]
    assert config["refIFO"] == "H1"
    assert config["gps_center"] == 1126259462.4
    assert config["accounting_group"] == "ligo.dev.o4.burst.allsky.cwboffline"
    assert config["channelNamesRaw"] == ["H1:GDS-CALIB_STRAIN", "L1:GDS-CALIB_STRAIN"]
    assert "TODO" in rendered  # DQF and other unfinished fields are flagged


def test_template_falls_back_when_no_data_files(tmp_path):
    production = FakeProduction(tmp_path, make_meta())
    pipeline = PyCWB(production)

    config_file = pipeline._render_config()
    with open(config_file) as f:
        config = yaml.safe_load(f)

    assert config["frFiles"] == []
    assert config["DQF"] == []


def test_template_uses_provided_data_files(tmp_path):
    # A single frame path per IFO (as e.g. a simple data-fetching pipeline
    # might provide) is written into a generated one-line cache file, since
    # pycWB's frFiles expects a *file* listing frame paths, not a raw path.
    meta = make_meta()
    meta["data"]["data files"] = {"H1": "input/H1-1234-32.gwf", "L1": "input/L1-1234-32.gwf"}
    production = FakeProduction(tmp_path, meta)
    pipeline = PyCWB(production)

    config_file = pipeline._render_config()
    with open(config_file) as f:
        config = yaml.safe_load(f)

    assert config["frFiles"] == [
        str(tmp_path / "frames_H1.in"),
        str(tmp_path / "frames_L1.in"),
    ]
    assert (tmp_path / "frames_H1.in").read_text() == "input/H1-1234-32.gwf\n"
    assert (tmp_path / "frames_L1.in").read_text() == "input/L1-1234-32.gwf\n"


def test_template_uses_provided_data_files_as_lists(tmp_path):
    # asimov-gwdata-style pipelines may store *multiple* frame paths per
    # IFO (a segment spanning several downloaded frame files) as a list;
    # each one must land on its own line in the generated cache file, not
    # be interpolated as a single Python-list-shaped string.
    meta = make_meta()
    meta["data"]["data files"] = {
        "H1": ["input/H1-1000-16.gwf", "input/H1-1016-16.gwf"],
        "L1": ["input/L1-1000-16.gwf", "input/L1-1016-16.gwf"],
    }
    production = FakeProduction(tmp_path, meta)
    pipeline = PyCWB(production)

    pipeline._render_config()

    assert (tmp_path / "frames_H1.in").read_text() == (
        "input/H1-1000-16.gwf\ninput/H1-1016-16.gwf\n"
    )
    assert (tmp_path / "frames_L1.in").read_text() == (
        "input/L1-1000-16.gwf\ninput/L1-1016-16.gwf\n"
    )


def test_missing_interferometers_raises(tmp_path):
    from asimov.pipeline import PipelineException

    meta = make_meta()
    meta.pop("interferometers")
    production = FakeProduction(tmp_path, meta)
    pipeline = PyCWB(production)

    try:
        pipeline._render_config()
        raised = False
    except PipelineException:
        raised = True

    assert raised


def test_missing_accounting_group_raises(tmp_path):
    from asimov.pipeline import PipelineException

    meta = make_meta()
    meta["scheduler"] = {}
    production = FakeProduction(tmp_path, meta)
    pipeline = PyCWB(production)

    try:
        pipeline._render_config()
        raised = False
    except PipelineException:
        raised = True

    assert raised


def test_find_existing_config_returns_none_without_repository(tmp_path):
    production = FakeProduction(tmp_path, make_meta())
    pipeline = PyCWB(production)

    assert pipeline._find_existing_config() is None


def test_find_existing_config_returns_none_when_file_missing(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    production = FakeProduction(tmp_path, make_meta(), repository=FakeRepository(repo_dir))
    pipeline = PyCWB(production)

    assert pipeline._find_existing_config() is None


def test_find_existing_config_finds_preseeded_yaml(tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / "analyses").mkdir(parents=True)
    preseeded = repo_dir / "analyses" / "pycwb-test.yaml"
    preseeded.write_text("outputDir: output\n")
    production = FakeProduction(tmp_path, make_meta(), repository=FakeRepository(repo_dir))
    pipeline = PyCWB(production)

    assert pipeline._find_existing_config() == str(preseeded)


def test_build_dag_dryrun_prefers_preseeded_config(tmp_path, capsys):
    repo_dir = tmp_path / "repo"
    (repo_dir / "analyses").mkdir(parents=True)
    preseeded = repo_dir / "analyses" / "pycwb-test.yaml"
    preseeded.write_text("outputDir: output\n")
    production = FakeProduction(tmp_path, make_meta(), repository=FakeRepository(repo_dir))
    pipeline = PyCWB(production)

    # dryrun=True only logs the resolved config path; it must not attempt to
    # render the template (which would overwrite the pre-seeded file) or
    # import pycWB.
    pipeline.build_dag(dryrun=True)

    assert preseeded.read_text() == "outputDir: output\n"


def test_detect_completion_false_with_only_empty_catalog(tmp_path):
    # pycWB's prepare_job_runs() (called from build_dag(), long before any
    # batch or merge job runs) already creates catalog/catalog.parquet as
    # an empty structure - detect_completion() must not treat that alone as
    # "finished".
    production = FakeProduction(tmp_path, make_meta())
    pipeline = PyCWB(production)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "catalog.parquet").write_bytes(b"")

    assert pipeline.detect_completion() is False


def test_detect_completion_true_once_progress_exists(tmp_path):
    # catalog/progress.parquet is only written by pycWB's merge DAG node,
    # once the batch job has recorded real per-lag progress.
    production = FakeProduction(tmp_path, make_meta())
    pipeline = PyCWB(production)
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    (catalog_dir / "catalog.parquet").write_bytes(b"")
    (catalog_dir / "progress.parquet").write_bytes(b"")

    assert pipeline.detect_completion() is True


def test_submit_dag_accepts_dryrun_kwarg(tmp_path):
    # asimov's own CLI unconditionally calls pipeline.submit_dag(dryrun=...)
    # (see e.g. asimov.cli.manage.submit) - a submit_dag(self) with no
    # parameter raises TypeError there, which is exactly what this plugin's
    # end-to-end test caught on a real asimov run.
    production = FakeProduction(tmp_path, make_meta())
    pipeline = PyCWB(production)
    pipeline.dag_filename = str(tmp_path / "pycwb-test.dag")
    open(pipeline.dag_filename, "w").close()

    assert pipeline.submit_dag(dryrun=True) is None
