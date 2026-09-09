"""Coverage for forge_web.py endpoint handlers, Job.cancel, and _wakewords_state
edge cases not already exercised by tests/test_forge_web.py and
tests/test_coverage_gaps.py.
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).parents[1]))
import forge
import forge_web


class Request:
    def __init__(self, body=None, name="demo", query=None, can_read_body=True):
        self.match_info = {"name": name}
        self.body = body or {}
        self.query = query or {}
        self.can_read_body = can_read_body

    async def json(self):
        return self.body


class RunningJob:
    """A job that never finishes — every conflict branch checks poll()."""
    label = "something else"

    def poll(self):
        return None


class JobCancelTests(unittest.TestCase):
    """The three escalation paths in Job.cancel(), none of which the
    existing "instant termination" test in tests/test_coverage_gaps.py
    reaches."""

    def _make_job(self, poll_returns):
        """A Job whose underlying process reports `poll_returns` in order,
        repeating the final value once exhausted."""
        class Proc:
            pid = 1
            def __init__(self):
                self._results = list(poll_returns)
            def poll(self):
                if len(self._results) > 1:
                    return self._results.pop(0)
                return self._results[0]
        proc = Proc()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(forge_web, "LOGS", Path(tmp) / "logs"), \
                    patch.object(forge_web.subprocess, "Popen", return_value=proc):
                job = forge_web.Job("build", "test", [])
        return job

    def test_cancel_on_an_already_finished_job_is_a_no_op(self):
        job = self._make_job([0])  # already exited before cancel() is called
        job.cancel()
        self.assertFalse(job.cancelled)  # never even attempted a signal

    def test_cancel_returns_quietly_when_the_process_group_is_already_gone(self):
        job = self._make_job([None, None])
        with patch.object(forge_web.os, "getpgid", side_effect=ProcessLookupError):
            job.cancel()
        self.assertTrue(job.cancelled)
        self.assertIsNone(job.rc)  # poll() was never re-checked past the early return
        job._logf.close()

    def test_cancel_waits_out_the_grace_period_then_escalates_to_sigkill(self):
        job = self._make_job([None, None, None, None])  # never reports finished
        monotonic_calls = iter([1000.0, 1000.02, 1000.10])  # deadline, in-grace, past-deadline
        with patch.object(forge_web, "CANCEL_GRACE_S", 0.05), \
                patch.object(forge_web.os, "getpgid", return_value=999), \
                patch.object(forge_web.time, "monotonic", side_effect=lambda: next(monotonic_calls)), \
                patch.object(forge_web.time, "sleep"), \
                patch.object(forge_web.os, "killpg", side_effect=[None, ProcessLookupError]) as killpg:
            job.cancel()
        self.assertTrue(job.cancelled)
        # First call was the SIGTERM, second the escalated SIGKILL — and the
        # SIGKILL raising ProcessLookupError (the process exited right as we
        # tried to escalate) must be swallowed, not propagated.
        self.assertEqual(killpg.call_count, 2)
        job._logf.close()


class WakewordsStateTests(unittest.TestCase):
    def test_returns_empty_when_the_wakewords_directory_does_not_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(forge, "WAKEWORDS", Path(tmp) / "missing"):
                self.assertEqual(forge_web._wakewords_state(), [])

    def test_an_unparseable_config_is_skipped_rather_than_crashing_the_whole_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "bad"; bad.mkdir()
            (bad / "config.yml").write_text(":\n  not: [valid, yaml")
            good = root / "good"; good.mkdir()
            (good / "config.yml").write_text(yaml.safe_dump(
                {"model_name": "good", "output_dir": str(root / "work")}))
            with patch.object(forge, "WAKEWORDS", root), \
                    patch.object(forge, "MODELS", root / "models"):
                words = forge_web._wakewords_state()
        self.assertEqual([w["name"] for w in words], ["good"])

    def test_an_invalid_training_mix_resolves_to_an_empty_dict_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_dir = root / "demo"; cfg_dir.mkdir()
            # At least one real clip is needed: with nothing on disk
            # resolve_training_mix short-circuits to "natural" before it
            # ever looks at the (invalid) requested weights.
            positive_dir = root / "work" / "demo" / "positive_train"
            positive_dir.mkdir(parents=True)
            (positive_dir / "piper_a.wav").write_bytes(b"wav")
            (cfg_dir / "config.yml").write_text(yaml.safe_dump({
                "model_name": "demo", "output_dir": str(root / "work"),
                # weights that don't sum to 100 make resolve_training_mix raise.
                "training_mix": {"positive": {"custom": 10, "piper": 10, "google": 10},
                                  "negative": {"custom": None, "piper": None, "google": None}},
            }))
            with patch.object(forge, "WAKEWORDS", root), patch.object(forge, "MODELS", root / "models"):
                words = forge_web._wakewords_state()
        self.assertEqual(words[0]["resolved_training_mix"], {})


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_ww, self.old_models = forge.WAKEWORDS, forge.MODELS
        forge.WAKEWORDS = self.root / "ww"
        forge.MODELS = self.root / "models"
        self.cfg_dir = forge.WAKEWORDS / "demo"
        self.cfg_dir.mkdir(parents=True)
        self.cfg_path = self.cfg_dir / "config.yml"
        self.cfg_path.write_text(yaml.safe_dump({
            "model_name": "demo", "output_dir": str(self.root / "work"),
        }))
        self.old_job = forge_web._job
        forge_web._job = None
        self.old_logs = forge_web.LOGS
        forge_web.LOGS = self.root / "logs"
        self.old_tmp = forge_web.TMP
        forge_web.TMP = self.root / "tmp"

    def tearDown(self):
        forge.WAKEWORDS, forge.MODELS = self.old_ww, self.old_models
        forge_web._job = self.old_job
        forge_web.LOGS = self.old_logs
        forge_web.TMP = self.old_tmp
        self.tmp.cleanup()

    # -- api_log --------------------------------------------------------
    async def test_log_reads_from_the_current_jobs_log_file_at_an_offset(self):
        log_path = self.root / "job.log"
        log_path.write_bytes(b"0123456789")
        forge_web._job = types.SimpleNamespace(log_path=log_path)
        response = await forge_web.api_log(Request(query={"offset": "3"}))
        payload = json.loads(response.body)
        self.assertEqual(payload["data"], "3456789")
        self.assertEqual(payload["offset"], 10)

    # -- api_job_cancel ---------------------------------------------------
    async def test_job_cancel_requires_a_running_job(self):
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_job_cancel(Request())

    async def test_job_cancel_invokes_cancel_off_the_event_loop(self):
        cancelled = []
        forge_web._job = types.SimpleNamespace(
            label="demo", poll=lambda: None, cancel=lambda: cancelled.append(True))
        response = await forge_web.api_job_cancel(Request())
        self.assertEqual(json.loads(response.body), {"ok": True, "cancelled": "demo"})
        self.assertEqual(cancelled, [True])

    # -- api_wakeword_create validation ----------------------------------
    async def test_wakeword_create_rejects_an_unknown_kind(self):
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_wakeword_create(Request({"kind": "sing", "phrase": "hi"}))

    async def test_wakeword_create_turns_a_cmd_new_system_exit_into_bad_request(self):
        with patch.object(forge, "cmd_new", side_effect=SystemExit("already exists")):
            with self.assertRaises(forge_web.web.HTTPBadRequest):
                await forge_web.api_wakeword_create(Request({"phrase": "hi"}))

    # -- _to_wav16k --------------------------------------------------------
    def test_to_wav16k_invokes_ffmpeg_with_the_16k_mono_s16_target(self):
        with patch.object(forge_web.subprocess, "run") as run:
            forge_web._to_wav16k(Path("in.webm"), Path("out.wav"))
        args = run.call_args.args[0]
        self.assertIn("-ar", args)
        self.assertIn("16000", args)
        self.assertTrue(run.call_args.kwargs.get("check"))

    # -- _save_uploads -----------------------------------------------------
    async def test_save_uploads_skips_fields_with_the_wrong_name(self):
        class Field:
            def __init__(self, name):
                self.name = name
                self.filename = "clip.webm"
                self._chunks = iter([b"data", b""])
            async def read_chunk(self):
                return next(self._chunks)

        class Reader:
            def __init__(self, fields):
                self._fields = iter(fields)
            def __aiter__(self):
                return self
            async def __anext__(self):
                try:
                    return next(self._fields)
                except StopIteration:
                    raise StopAsyncIteration

        req = Request()
        async def multipart():
            return Reader([Field("other"), Field("dataset")])
        req.multipart = multipart
        paths = await forge_web._save_uploads(req, "dataset")
        self.assertEqual(len(paths), 1)
        paths[0].unlink()

    # -- api_import_dataset --------------------------------------------
    async def test_import_dataset_conflicts_while_a_job_is_running(self):
        forge_web._job = RunningJob()
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_import_dataset(Request())

    async def test_import_dataset_full_success_and_error_paths(self):
        upload = self.root / "upload.zip"
        upload.write_bytes(b"zip")
        with patch.object(forge_web, "_save_uploads", return_value=[upload]), \
                patch.object(forge, "import_labeled_dataset", return_value={"positive": 3}) as importer:
            response = await forge_web.api_import_dataset(Request())
        self.assertEqual(json.loads(response.body)["counts"], {"positive": 3})
        importer.assert_called_once()
        self.assertFalse(upload.exists())  # cleaned up in the `finally`

        with patch.object(forge_web, "_save_uploads", return_value=[]):
            with self.assertRaises(forge_web.web.HTTPBadRequest):
                await forge_web.api_import_dataset(Request())

        upload2 = self.root / "upload2.zip"; upload2.write_bytes(b"zip")
        with patch.object(forge_web, "_save_uploads", return_value=[upload2]), \
                patch.object(forge, "import_labeled_dataset", side_effect=FileNotFoundError("no such wakeword")):
            with self.assertRaises(forge_web.web.HTTPNotFound):
                await forge_web.api_import_dataset(Request())
        self.assertFalse(upload2.exists())

        upload3 = self.root / "upload3.zip"; upload3.write_bytes(b"zip")
        with patch.object(forge_web, "_save_uploads", return_value=[upload3]), \
                patch.object(forge, "import_labeled_dataset", side_effect=ValueError("bad zip")):
            with self.assertRaises(forge_web.web.HTTPBadRequest):
                await forge_web.api_import_dataset(Request())
        self.assertFalse(upload3.exists())

    # -- conflict-while-job-running branches shared by several endpoints --
    async def test_conflict_while_job_running_across_config_endpoints(self):
        forge_web._job = RunningJob()
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_dataset_options(Request())
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_training_mix(Request({"training_mix": {}}))
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_confusables(Request({"phrases": []}))
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_prune_google_tts(Request({}))
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_delete_piper_samples(Request({}))

    # -- api_training_mix validation --------------------------------------
    async def test_training_mix_rejects_a_non_object_mix_and_bad_weights(self):
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_training_mix(Request({"training_mix": "nope"}))
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_training_mix(Request({"training_mix": {"positive": "nope", "negative": {}}}))
        # Weights that don't sum to 100 surface as 400, not a 500 — but only
        # once there is at least one clip to draw from; with nothing on disk
        # resolve_training_mix short-circuits to "natural" before it ever
        # looks at the requested weights.
        work = self.root / "work" / "demo" / "positive_train"
        work.mkdir(parents=True)
        (work / "piper_a.wav").write_bytes(b"wav")
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_training_mix(Request({"training_mix": {
                "positive": {"custom": 10, "piper": 10, "google": 10},
                "negative": {"custom": None, "piper": None, "google": None},
            }}))

    # -- api_google_tts validation ------------------------------------------
    async def test_google_tts_rejects_bad_samples_and_qps(self):
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_google_tts(Request({"samples": "nope"}))
        # `body.get("samples") or default` treats 0 as "not provided", so a
        # genuinely negative value is what actually exercises "samples < 1".
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_google_tts(Request({"samples": -5}))
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_google_tts(Request({"samples": 5, "qps": "nope"}))
        with self.assertRaises(forge_web.web.HTTPBadRequest):
            await forge_web.api_google_tts(Request({"samples": 5, "qps": -1}))

    # -- api_prune_google_tts error surfaces --------------------------------
    async def test_prune_google_tts_surfaces_voice_resolution_errors(self):
        import google_tts
        with patch.object(google_tts, "selected_chirp3_pairs", side_effect=ValueError("bad locale")):
            with self.assertRaises(forge_web.web.HTTPBadRequest):
                await forge_web.api_prune_google_tts(Request({}))
        with patch.object(google_tts, "selected_chirp3_pairs", side_effect=RuntimeError("quota")):
            with self.assertRaises(forge_web.web.HTTPServiceUnavailable):
                await forge_web.api_prune_google_tts(Request({}))

    # -- api_google_tts_voices ------------------------------------------
    async def test_google_tts_voices_success_and_failure(self):
        import google_tts
        with patch.object(google_tts, "list_chirp3_voices", return_value={"en-US": ["A"]}):
            response = await forge_web.api_google_tts_voices(Request(query={"languages": "en-US"}))
        self.assertEqual(json.loads(response.body), {"voices": {"en-US": ["A"]}})
        with patch.object(google_tts, "list_chirp3_voices", side_effect=RuntimeError("no credentials")):
            with self.assertRaises(forge_web.web.HTTPServiceUnavailable):
                await forge_web.api_google_tts_voices(Request())

    # -- api_delete_piper_samples OSError -----------------------------------
    async def test_delete_piper_samples_surfaces_an_unlink_failure(self):
        work = self.root / "work" / "demo" / "positive_train"
        work.mkdir(parents=True)
        clip = work / "piper_gb_000000_v_s0.wav"
        clip.write_bytes(b"wav")
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            with self.assertRaises(forge_web.web.HTTPInternalServerError):
                await forge_web.api_delete_piper_samples(Request())

    # -- api_voices / api_preview upstream failures -------------------------
    async def test_voices_surfaces_a_catalogue_failure_as_bad_gateway(self):
        piper = types.ModuleType("piper_voices")
        piper.catalogue = lambda assets: (_ for _ in ()).throw(RuntimeError("network down"))
        piper.languages = lambda assets: []
        old = sys.modules.get("piper_voices")
        sys.modules["piper_voices"] = piper
        try:
            with self.assertRaises(forge_web.web.HTTPBadGateway):
                await forge_web.api_voices(Request(query={"language": "en"}))
        finally:
            if old is None:
                sys.modules.pop("piper_voices", None)
            else:
                sys.modules["piper_voices"] = old

    async def test_preview_surfaces_a_synthesis_failure_as_bad_gateway(self):
        with patch.object(forge_web.asyncio, "to_thread", side_effect=RuntimeError("no voice")):
            with self.assertRaises(forge_web.web.HTTPBadGateway):
                await forge_web.api_preview(Request({"text": "hello"}))

    # -- api_evaluate: model missing --------------------------------------
    async def test_evaluate_requires_a_built_model(self):
        with self.assertRaises(forge_web.web.HTTPConflict):
            await forge_web.api_evaluate(Request())

    # -- static file handlers ------------------------------------------------
    async def test_index_and_live_mic_worklet_serve_static_files(self):
        index_response = await forge_web.index(Request())
        self.assertIsInstance(index_response, forge_web.web.FileResponse)
        worklet_response = await forge_web.live_mic_worklet(Request())
        self.assertIsInstance(worklet_response, forge_web.web.FileResponse)


if __name__ == "__main__":
    unittest.main()
