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


class FakeEvent:
    def __init__(self, name):
        self.name = name


class FakeProduction:
    """A minimal stand-in for asimov.analysis.Analysis, just enough for
    asimov.pipeline.Pipeline.__init__ and PyCWB._render_config to run."""

    def __init__(self, rundir, meta):
        self.event = FakeEvent("GW150914_095045")
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
    meta = make_meta()
    meta["data"]["data files"] = {"H1": "input/H1_frames.in", "L1": "input/L1_frames.in"}
    production = FakeProduction(tmp_path, meta)
    pipeline = PyCWB(production)

    config_file = pipeline._render_config()
    with open(config_file) as f:
        config = yaml.safe_load(f)

    assert config["frFiles"] == ["input/H1_frames.in", "input/L1_frames.in"]


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
