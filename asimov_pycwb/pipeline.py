"""
Asimov pipeline plugin wrapping pycWB's targeted (single-candidate) mode.

pycWB (https://github.com/PycWB/pycwb) is primarily a continuous, all-sky
*search* pipeline: it scans a long stretch of data for triggers, which has no
natural mapping onto asimov's per-Production model (one analysis of one
known event). This plugin does **not** attempt to run that mode.

Instead it targets pycWB's single-candidate reconstruction mode, in which a
short segment of data centred on a known trigger time (``gps_center`` +/-
``time_left``/``time_right``) is run through the same search-configuration
machinery to produce a sky localisation and waveform reconstruction for that
one candidate. See this repository's README.md for the full rationale.
"""

import glob
import os
import shutil

from importlib.resources import files

from liquid import Liquid

from asimov.pipeline import Pipeline, PipelineException


class PyCWB(Pipeline):
    """
    The pycWB pipeline, wrapping its targeted/single-candidate follow-up mode.
    """

    name = "pycwb"
    config_template = str(
        files("asimov_pycwb").joinpath("templates/user_parameters.yaml.liquid")
    )

    def __init__(self, production, category=None):
        super().__init__(production, category)
        self.logger.info(
            "Using the pycWB pipeline (targeted single-candidate reconstruction mode)"
        )
        self.dag_filename = None

    # -- helpers -------------------------------------------------------

    @property
    def config_filename(self):
        return os.path.join(self.production.rundir, "user_parameters.yaml")

    def _find_existing_config(self):
        """
        Look for a ``user_parameters.yaml`` already checked into the event
        repository for this production (e.g. ``checkouts/<EVENT>/analyses/
        <production name>.yaml``), so a hand-written config can be used
        as-is instead of being rendered from the liquid template.

        asimov's own ``Analysis.event.repository.find_prods()`` always
        assumes a ``.ini`` extension, so it can't be reused directly for
        pycWB's YAML config; this mirrors its category/path convention
        with a ``.yaml`` extension instead. Returns ``None`` if there is no
        repository, or no matching file exists yet.
        """
        repository = getattr(self.production.event, "repository", None)
        if not repository:
            return None

        category = self.category or "analyses"
        candidate = os.path.join(
            repository.directory, category, f"{self.production.name}.yaml"
        )
        return candidate if os.path.exists(candidate) else None

    def _write_frame_cache_files(self, ifos, data_files):
        """
        Write a per-IFO frame-cache list file for pycWB's ``frFiles``.

        pycWB's ``frFiles`` expects, per IFO, the path to a text file
        listing one frame path per line (see
        ``pycwb.modules.job_segment.frame.get_frame_meta``) - not raw frame
        paths embedded directly in the config. Data-fetching pipelines
        (e.g. asimov-gwdata) may store either a single frame path or a
        list of frame paths per IFO in
        ``production.meta['data']['data files']``; this normalizes either
        shape into a real cache-list file pycWB can read, so the template
        never has to interpolate a Python list into a YAML scalar.

        Returns a dict of ``{ifo: cache_file_path}`` for any IFO with data
        files provided; IFOs without one are omitted (the template falls
        back to its "download data" branch for those).
        """
        cache_files = {}
        for ifo in ifos:
            paths = data_files.get(ifo)
            if not paths:
                continue
            if isinstance(paths, str):
                paths = [paths]
            cache_file = os.path.join(self.production.rundir, f"frames_{ifo}.in")
            with open(cache_file, "w") as f:
                for path in paths:
                    f.write(f"{path}\n")
            cache_files[ifo] = cache_file
        return cache_files

    def _render_config(self):
        """
        Render this production's ``user_parameters.yaml`` from the packaged
        liquid template and asimov's production metadata.

        Only the run/segment/IFO/data fields are templated here; the cWB
        threshold and regulator parameters in the template are fixed
        defaults (see the template's own comments and this plugin's
        README.md for the list of TODOs).
        """
        os.makedirs(self.production.rundir, exist_ok=True)

        meta = self.production.meta

        ifos = meta.get("interferometers")
        if not ifos:
            raise PipelineException(
                "No interferometers were specified in "
                "production.meta['interferometers']",
                production=self.production,
            )

        event_time = meta.get("event time")
        if event_time is None:
            raise PipelineException(
                "No event time was specified in production.meta['event time']",
                production=self.production,
            )

        scheduler_meta = meta.get("scheduler", {})
        accounting_group = scheduler_meta.get("accounting group")
        if not accounting_group:
            raise PipelineException(
                "No accounting group was specified in "
                "production.meta['scheduler']['accounting group']",
                production=self.production,
            )

        data_meta = meta.get("data", {})
        quality_meta = meta.get("quality", {})
        likelihood_meta = meta.get("likelihood", {})
        raw_data_files = data_meta.get("data files", {})

        # Targeted follow-up window: default to a symmetric window built
        # from the (deprecated pesummary-style) "segment length" meta key
        # if it's set, otherwise a 20-minute-per-side window, which is
        # comfortably larger than the fixed segLen/segMLS defaults below.
        window = data_meta.get("segment length", 2400)
        time_left = data_meta.get("time before", window / 2)
        time_right = data_meta.get("time after", window / 2)

        min_freq = likelihood_meta.get("minimum frequency", {})
        max_freq = likelihood_meta.get("maximum frequency", {})
        f_low = min(min_freq.values()) if min_freq else 16.0
        f_high = max(max_freq.values()) if max_freq else 1024.0

        context = dict(
            n_proc=scheduler_meta.get("n proc", 1),
            ifos=ifos,
            ref_ifo=meta.get("reference ifo", ifos[0]),
            event_time=event_time,
            time_left=time_left,
            time_right=time_right,
            f_low=f_low,
            f_high=f_high,
            channels=data_meta.get("channels", {}),
            data_files=self._write_frame_cache_files(ifos, raw_data_files),
            veto_files=quality_meta.get("veto files", {}),
            accounting_group=accounting_group,
            conda_env=scheduler_meta.get("conda environment", ""),
            job_memory=scheduler_meta.get("request memory", "6GB"),
            job_disk=scheduler_meta.get("request disk", "8GB"),
        )

        liq = Liquid(self.config_template)
        rendered = liq.render(**context)

        with open(self.config_filename, "w") as config_file:
            config_file.write(rendered)

        return self.config_filename

    # -- Pipeline interface ----------------------------------------------

    def build_dag(self, dryrun=False):
        """
        Render this production's pycWB config and build (but do not submit)
        an HTCondor DAG for its targeted reconstruction run.

        If a ``user_parameters.yaml`` already exists for this production in
        the event repository, it is used as-is instead of being rendered
        from the template - this is the escape hatch for anything the
        template doesn't (yet) cover, such as injections into synthetic
        noise (see ``_find_existing_config`` and this plugin's README).
        """
        config_file = self._find_existing_config() or self._render_config()

        if dryrun:
            self.logger.info(f"Dry run: rendered pycWB config at {config_file}")
            return

        # Imported lazily so that importing this module (e.g. for the
        # template-rendering unit tests) doesn't require pycWB's compiled
        # cwb-core/ROOT dependencies to be installed.
        from pycwb.modules.condor.condor import HTCondor
        from pycwb.workflow.subflow.prepare_job_runs import prepare_job_runs

        working_dir = os.path.abspath(self.production.rundir)

        # HTCondor.create() interactively confirms before touching an
        # existing condor/ directory, which would hang a non-interactive
        # asimov run. Since build_dag() is meant to regenerate the DAG from
        # scratch, clear it ourselves first - along with every directory
        # that holds run *state* (as opposed to shared, expensive-to-rebuild
        # inputs like the downloaded wdmXTalk/ catalog). pycWB's own
        # prepare_job_runs(..., overwrite=True) is a resume feature: it
        # happily reuses an existing catalog/progress/trigger/output from a
        # previous run, which would make detect_completion() see stale
        # "already complete" state from before this rebuild rather than
        # this run's own progress.
        for stale_dir in ("condor", "catalog", "trigger", "output", "job_status", "log"):
            stale_path = os.path.join(working_dir, stale_dir)
            if os.path.exists(stale_path):
                shutil.rmtree(stale_path)

        scheduler_meta = self.production.meta.get("scheduler", {})
        n_proc = scheduler_meta.get("n proc", 1)

        # prepare_job_runs() does os.chdir(working_dir) and never restores
        # the original directory - confirmed directly by this plugin's own
        # end-to-end test, where that leaked into asimov's own subsequent
        # CLI processing (a FileNotFoundError writing its relative-path
        # ".asimov/_cache_jobs.yaml" cache from what was now pycWB's rundir
        # instead of the asimov project root). Restore it ourselves.
        original_cwd = os.getcwd()
        try:
            job_segments, config, working_dir = prepare_job_runs(
                working_dir, config_file, n_proc=n_proc, overwrite=True
            )

            condor = HTCondor(
                working_dir=working_dir,
                conda_env=scheduler_meta.get("conda environment") or None,
                accounting_group=scheduler_meta.get("accounting group"),
                n_proc=n_proc,
                memory=scheduler_meta.get("request memory", "6GB"),
                disk=scheduler_meta.get("request disk", "8GB"),
            )
            condor.create(job_segments, submit=False)
        finally:
            os.chdir(original_cwd)

        # pycWB's HTCondor.create() unconditionally writes "use_oauth_services
        # = scitokens" plus a matching BEARER_TOKEN_FILE environment line into
        # every node's submit file (batch, merge, simulation_summary) - there
        # is no config option to skip this (confirmed directly against
        # pycwb.modules.condor.condor). On a real IGWN pool with a working
        # SciTokens credmon this is exactly what's needed to read real frame
        # data; on a pool without one configured, HTCondor holds every job
        # with "Job credentials are not available" and they never run. This
        # is for test/local pools that have no credmon at all (e.g. this
        # plugin's own end-to-end test, which uses only synthetic noise and
        # needs no real credentials) - it must stay off by default so real
        # deployments keep requesting real credentials.
        if scheduler_meta.get("strip oauth credentials", False):
            for sub_file in glob.glob(os.path.join(working_dir, "condor", "*.sub")):
                with open(sub_file) as f:
                    lines = f.readlines()
                with open(sub_file, "w") as f:
                    f.writelines(
                        line
                        for line in lines
                        if not line.strip().startswith("use_oauth_services")
                        and not line.strip().startswith("environment = BEARER_TOKEN_FILE")
                    )

        # condor.dag_file is a pathlib.Path (from htcondor2.dags.write_dag);
        # asimov's own scheduler.submit_dag() passes it straight to
        # htcondor2.Submit.from_dag(), which requires a plain str.
        self.dag_filename = str(condor.dag_file)
        self.logger.info(f"Built pycWB condor DAG at {self.dag_filename}")

    def submit_dag(self, dryrun=False):
        """
        Submit this production's pre-built DAG to the configured scheduler.

        ``dryrun`` is accepted (rather than defaulting to no arguments) to
        match asimov's own CLI, which unconditionally calls
        ``pipeline.submit_dag(dryrun=dryrun)`` (see e.g.
        ``asimov.cli.manage.submit``).
        """
        if not self.dag_filename or not os.path.exists(self.dag_filename):
            raise PipelineException(
                "No DAG file has been built for this production; "
                "run build_dag() first.",
                production=self.production,
            )

        if dryrun:
            self.logger.info(f"Dry run: would submit DAG at {self.dag_filename}")
            return None

        # HTCondor resolves the relative submit-file paths inside a DAG
        # (pycWB's own condor.py writes e.g. "JOB pycwb_batch:0
        # pycwb_batch.sub", with no directory prefix) against the
        # submitting process's own working directory at submission time -
        # not against the .dag file's directory (that's what -usedagdir is
        # for, which asimov's scheduler doesn't pass). Confirmed directly:
        # submitting from asimov's own project root left DAGMan unable to
        # open "pycwb_batch.sub" at all (errno=2), aborting the DAG before
        # any node ever queued. Submit from the DAG's own directory instead.
        original_cwd = os.getcwd()
        try:
            os.chdir(os.path.dirname(self.dag_filename))
            cluster_id = self.scheduler.submit_dag(
                self.dag_filename,
                batch_name=f"pycwb/{self.production.event.name}/{self.production.name}",
            )
        finally:
            os.chdir(original_cwd)
        self.production.job_id = cluster_id
        self.production.status = "running"
        return cluster_id

    def detect_completion(self):
        """
        A pycWB run is complete once its merge job has produced
        ``catalog/progress.parquet``.

        ``catalog/catalog.parquet`` is *not* a reliable signal on its own:
        pycWB's own ``prepare_job_runs()`` (called from ``build_dag()``,
        long before any batch or merge job runs) already creates this file
        as an empty structure via ``Catalog.create()``, so checking only
        for its existence would mark a production "finished" immediately
        after ``build_dag()``, before pycWB has actually run anything.
        ``catalog/progress.parquet`` is a distinct file that only pycWB's
        merge DAG node writes (via ``merge_progress()``), and only once the
        batch job has recorded real per-lag progress - a genuine signal
        that the merge step actually ran.
        """
        progress_file = os.path.join(
            self.production.rundir, "catalog", "progress.parquet"
        )
        return os.path.exists(progress_file)

    def collect_assets(self):
        """
        Gather the analysis assets produced by this production's run.

        This is best-effort until validated against a real run: the merged
        catalog and per-trigger sky-map/waveform outputs are all pycWB
        writes for a targeted run, and their exact layout may need
        adjusting once this plugin has been exercised end-to-end.
        """
        rundir = self.production.rundir
        assets = {}

        catalog_file = os.path.join(rundir, "catalog", "catalog.parquet")
        if os.path.exists(catalog_file):
            assets["catalog"] = catalog_file

        # TODO: pycWB writes sky-map statistics as a per-trigger JSON file
        # (`skymap_statistics.json`, when `save_sky_map` is set), not as a
        # FITS file. Asimov's base Pipeline.store_results() expects a
        # `{production.name}_skymap.fits` file, which would be a natural
        # fit for cWB's sky localisation output - but converting the JSON
        # into an actual FITS skymap (e.g. via ligo.skymap/healpy) isn't
        # implemented yet.
        skymap_files = sorted(
            glob.glob(os.path.join(rundir, "trigger", "*", "skymap_statistics.json"))
        )
        if skymap_files:
            assets["skymap_statistics"] = skymap_files

        # TODO: the DAG built by build_dag() only merges the catalog and
        # progress files (`pycwb merge`, without `--wave`); per-job
        # waveform reconstruction files are left unmerged in output/. A
        # `pycwb merge --wave` step (e.g. in an after_completion() hook)
        # would be needed to produce a single merged wave.h5.
        waveform_files = sorted(glob.glob(os.path.join(rundir, "output", "wave_*.h5")))
        if waveform_files:
            assets["waveforms"] = waveform_files

        return assets
