"""Closed-loop controller (model-predictive): plan -> approve -> execute in slices ->
measure -> compare -> correct -> re-plan.

For each protocol step:
  1. the optimizer plans the step from the BELIEVED state
  2. the safety checker approves (or blocks) the plan
  3. the executor sends it to the adapter in small time slices
  4. after each slice the sensors are read and compared with the model's prediction
  5. if the error is above control.error_threshold, the model's parameters are refitted
     (mesh resistance from pressure, chamber volume from outlet absorbance) and the rest
     of the step is re-planned
  6. predictions, measurements, errors, model updates and alerts are all logged
With sensors, the believed state is corrected by each measurement and converges on the
truth. Without sensors (open loop) belief runs on the model alone and drifts; the
summary reports how far.

Operators (operators.py, llm_operator.py) drive this through a small set of primitives:
plan_step, execute_plan, back_flush, get_state, get_sensors, get_alerts,
pause_for_human, distrust_sensor. Every action passes the safety checker.
"""

from __future__ import annotations

import math

import optimizer
import planner
import safety
from model import CARRIER, ModelParams, ProcessModel
from state import ChipState

# Which args go into the run file's event for each command, in the order of
# contracts/schemas/commands.schema.json (an event is validated against it).
EVENT_ARG_KEYS = {
    "load": ("target", "volume_ul"),
    "exchange": ("target", "reagent", "volume_ul", "washes", "flow_ul_min", "direction"),
    "hold_temp": ("target", "temp_c", "duration_s"),
    "release": ("target", "reagent"),
    "split": ("source", "targets"),
    "pool": ("sources", "target"),
    "culture": ("targets", "reagents", "factors", "duration_s", "exchange_interval_s", "volume_ul",
                "flow_ul_min", "temp_c"),
    "collect": ("targets", "volume_ul"),
    "harvest": ("targets", "reagent"),
}

# Which sensor a model refit is evidence FROM, so distrusting that sensor can undo it.
FIT_SENSOR = {"mesh_resistance": "pressure_kpa", "chamber_volume_ul": "outlet_absorbance"}


class Controller:
    def __init__(self, protocol: dict, chip: dict, adapter, scenario: dict | None = None,
                 operator: str = "planner", monitor=None):
        self.protocol, self.chip, self.adapter = protocol, chip, adapter
        self.scenario = scenario or {}
        self.operator = operator
        self.params, self.materials = safety._contracts()
        self.model = ProcessModel(ModelParams.from_chip(chip, self.params))
        self.initial_model = self.model.params.copy()
        reagents = sorted({s["reagent"] for s in protocol["steps"] if s.get("reagent")} | {CARRIER})
        self.reagents = reagents
        self.believed = ChipState.from_chip_spec(chip, self.params, reagents=reagents,
                                                 reservoirs_ul=self.scenario.get("reservoirs_ul"), label="believed")
        self.initial_reservoirs = dict(self.believed.reservoirs_ul)
        # The run's own parameters (e.g. variant = control | interaction). Without these the
        # safety checker's variant rules cannot fire at all, which is how a closed-loop run
        # on a control chip used to be approved by a checker that never saw the variant.
        self.run_params = planner.run_params_for(chip, self.scenario.get("run_parameters"))
        # Chambers whose contents are deliberately killed later in the protocol, and when:
        # a rinse before that step is a PRE-LYSIS rinse and has its own shear ceiling.
        self.lyse_steps = self._lyse_steps(protocol)
        caps = adapter.capabilities()
        self.trusted = set(caps.get("sensors", []))
        # Some adapters answer read_sensors() with the model's own predictions (the dry run
        # does). Those are NOT measurements: they must never be fused into belief, refit the
        # model or be printed in a column called "measured".
        self.readings_are_predictions = bool(caps.get("readings_are_predictions"))
        if self.readings_are_predictions:
            self.trusted = set()
        self.loop = "closed" if self.trusted else "open"
        p = self.params["parameters"]
        self.error_threshold = p["control.error_threshold"]["value"]
        self.slices_per_wash = int(p["control.slices_per_wash"]["value"])
        self.monitor = monitor
        self.steps = {s["step"]: s for s in protocol["steps"]}
        self.plans: dict[int, dict] = {}
        self.events: list[dict] = []
        self.log: list[dict] = []
        self.alerts: list[dict] = []          # all alerts raised
        self.active_alerts: list[dict] = []   # not yet handled
        self.model_updates: list[dict] = []
        self.transcript: list[dict] = []      # every operator action, tool-call style
        self.last_measurement: dict = {}
        self.fits: list[dict] = []            # model refits, with the sensor each rests on
        self.conservation_fixes: list[dict] = []
        self.status = "running"
        self.status_reason = ""
        self.current_step: int | None = None
        self.step_results: dict[int, dict] = {}
        self.recoveries = 0
        # Absorbance evidence accumulates over an exchange: (target, reagent, c at start, volume since).
        self._anchor: dict | None = None

    @staticmethod
    def _lyse_steps(protocol: dict) -> dict:
        """chamber -> the first step number at which its contents are deliberately killed."""
        out: dict[str, int] = {}
        for s in protocol["steps"]:
            name = str(s.get("reagent") or "").lower()
            if any(w in name for w in safety.LYSE_WORDS):
                for t in [s.get("target")] + list(s.get("targets") or []):
                    if t:
                        out.setdefault(t, s["step"])
        return out

    def _lyse_ahead(self, step_no: int | None, args: dict) -> bool:
        """True when this command acts on a chamber that is lysed at or after this step."""
        targets = [t for t in [args.get("target")] + list(args.get("targets") or []) if t]
        at = step_no if step_no is not None else -1
        return any(t in self.lyse_steps and at <= self.lyse_steps[t] for t in targets)

    def _check(self, command: str, args: dict, step_no: int | None = None):
        """Every safety check in this class goes through here, so no call site can forget
        the run parameters (rule 7) or the pre-lysis context (rule 9)."""
        return safety.check(command, args, self.believed, self.chip, self.model,
                            run_params=self.run_params, lyse_ahead=self._lyse_ahead(step_no, args))

    # ------------------------------------------------------------------ logging

    def _log(self, kind: str, message: str, step: int | None = None, **data) -> dict:
        entry = {"t_s": round(self.believed.clock_s, 2), "kind": kind, "step": step if step is not None else self.current_step,
                 "message": message}
        if data:
            entry["data"] = data
        self.log.append(entry)
        return entry

    def record_call(self, tool: str, args: dict, explanation: str) -> None:
        self.transcript.append({"tool": tool, "input": args, "explanation": explanation})

    # ------------------------------------------------------------------ primitives

    def get_state(self) -> dict:
        return {"loop": self.loop, "status": self.status,
                "chambers": {cid: self.believed.summary(cid) for cid in self.believed.chambers},
                "model": {"mesh_resistance": dict(self.model.params.mesh_resistance),
                          "chamber_volume_ul": dict(self.model.params.chamber_volume_ul)},
                "trusted_sensors": sorted(self.trusted)}

    def get_sensors(self) -> dict:
        return dict(self.last_measurement)

    def get_alerts(self) -> list[dict]:
        return list(self.active_alerts)

    def plan_step(self, step_no: int, explanation: str = "") -> dict:
        step = self.steps[step_no]
        self.current_step = step_no
        self.record_call("plan_step", {"step": step_no}, explanation)
        goal = optimizer.goal_for_step(step, self.chip)
        plan = optimizer.plan_step(goal, self.believed, self.model, self.chip, run_params=self.run_params,
                                   lyse_ahead=self._lyse_ahead(step_no, goal.get("args") or goal))
        self.plans[step_no] = plan
        chosen = plan["args"]
        msg = (f"step {step_no}: chose {self._describe(plan['command'], chosen)}; predicted {plan['predicted']}"
               if plan["feasible"] else f"step {step_no}: no safe plan meets the goal")
        self._log("plan", msg, step_no, plan=plan, explanation=explanation)
        return plan

    def execute_plan(self, step_no: int, explanation: str = "") -> dict:
        self.record_call("execute_plan", {"step": step_no}, explanation)
        plan = self.plans.get(step_no) or self.plan_step(step_no, explanation)
        if not plan["feasible"]:
            self._log("blocked", f"step {step_no}: nothing to execute (no safe plan)", step_no)
            return {"status": "no_safe_plan"}
        verdict = self._check(plan["command"], plan["args"], step_no)
        self._log("safety", f"{verdict.status}: {self._describe(plan['command'], plan['args'])}"
                  + (f" ({'; '.join(verdict.reasons)})" if verdict.reasons else ""), step_no, safety=verdict.to_dict(),
                  explanation=explanation)
        if verdict.status == "blocked":
            return {"status": "blocked", "reasons": verdict.reasons}
        outcome = self._run(plan, step_no, self.steps[step_no].get("note", ""), verdict,
                            explanation=explanation or self.rationale(plan))
        if outcome["status"] == "done":
            self.step_results[step_no] = self._step_result(step_no)
        return outcome

    def back_flush(self, chamber: str, explanation: str = "", reagent: str | None = None) -> dict:
        """Lift capsules off the mesh (recovery from a pressure alert, or a protocol step)."""
        self.record_call("back_flush", {"chamber": chamber}, explanation)
        # Mid-wash recovery uses the wash's own buffer, so the exchange carries on where it was.
        cur = self.steps.get(self.current_step) or {}
        if not reagent and cur.get("operation") == "exchange" and cur.get("reagent"):
            reagent = cur["reagent"]
        reagent = reagent or next((s["reagent"] for s in self.protocol["steps"]
                                   if s.get("direction") == "backflush" and s.get("reagent")), None) \
            or next((s["reagent"] for s in self.protocol["steps"] if s["operation"] == "exchange"), "wash_buffer")
        goal = {"step": self.current_step, "operation": "exchange", "kind": "backflush", "target": chamber,
                "reagent": reagent, "volume_ul": round(2 * self.model.params.chamber_volume_ul[chamber], 1),
                "fraction": optimizer._param("backflush.target_fraction")}
        plan = optimizer.plan_step(goal, self.believed, self.model, self.chip, run_params=self.run_params,
                                   lyse_ahead=self._lyse_ahead(self.current_step, goal))
        self._log("plan", f"recovery back-flush of {chamber}: {self._describe(plan['command'], plan['args'] or {})}",
                  plan=plan, explanation=explanation)
        if not plan["feasible"]:
            return {"status": "no_safe_plan"}
        verdict = self._check(plan["command"], plan["args"], self.current_step)
        self._log("safety", f"{verdict.status}: recovery back-flush", safety=verdict.to_dict(), explanation=explanation)
        if verdict.status == "blocked":
            return {"status": "blocked", "reasons": verdict.reasons}
        outcome = self._run(plan, self.current_step, "Recovery: back-flush to lift the capsules and clear the mesh.",
                            verdict, recovery=True, explanation=explanation or self.rationale(plan))
        self.recoveries += 1
        for a in self.active_alerts:
            if a["name"] == "pressure_high":
                a["resolved_by"] = "back_flush"
        self.active_alerts = [a for a in self.active_alerts if a["name"] != "pressure_high"]
        return outcome

    def pause_for_human(self, reason: str) -> dict:
        self.record_call("pause_for_human", {"reason": reason}, reason)
        self.status, self.status_reason = "paused_for_human", reason
        self._log("pause", f"paused for a human: {reason}")
        return {"status": "paused_for_human"}

    def stop(self, reason: str) -> dict:
        self.status, self.status_reason = "stopped", reason
        self._log("stop", f"stopped: {reason}")
        return {"status": "stopped"}

    def distrust_sensor(self, sensor: str, reason: str) -> dict:
        self.record_call("distrust_sensor", {"sensor": sensor}, reason)
        self.trusted.discard(sensor)
        rolled = self._rollback_fits(sensor)
        for a in self.active_alerts:
            if a["name"] == "sensor_disagreement" and a["evidence"].get("sensor") == sensor:
                a["resolved_by"] = "distrust_sensor"
        self.active_alerts = [a for a in self.active_alerts
                              if not (a["name"] == "sensor_disagreement" and a["evidence"].get("sensor") == sensor)]
        self._log("model_update", f"distrusting sensor {sensor}; falling back to the model for it ({reason})"
                  + (f"; rolled back {len(rolled)} model update(s) that rested on it: "
                     + ", ".join(rolled) if rolled else ""), sensor=sensor, rolled_back=rolled)
        return {"status": "ok", "trusted_sensors": sorted(self.trusted), "model_rolled_back": rolled}

    def _rollback_fits(self, sensor: str) -> list[str]:
        """Undo every model refit that rested on `sensor`, newest first.

        A distrusted sensor's readings were never evidence, so the parameters they moved
        have to go back. Without this a stuck or wild pressure sensor left mesh_resistance
        permanently too low, the safety checker went on under-predicting mesh pressure from
        that moment, and it would approve flows above capsule.escape_pressure_kpa - with
        the sensor now distrusted, nothing left to catch it.
        """
        rolled = []
        keep = []
        for fit in reversed(self.fits):
            if fit["sensor"] != sensor:
                keep.append(fit)
                continue
            getattr(self.model.params, fit["attr"])[fit["key"]] = fit["old"]
            rolled.append(f"{fit['attr']}[{fit['key']}] back to {fit['old']:.4g}")
            self._model_update(f"{fit['attr']}[{fit['key']}]", fit["new"], fit["old"], fit["unit"],
                               f"rolled back: it was fitted from {sensor}, which is no longer trusted",
                               sensor=sensor, record_fit=False)
        self.fits = list(reversed(keep))
        return rolled

    # ------------------------------------------------------------------ execution

    def _describe(self, command: str, args: dict) -> str:
        if not command:
            return "nothing"
        if command == "exchange":
            d = " back-flush" if args.get("direction") == "backflush" else ""
            soak = f" + {args['soak_s']:.0f} s soak" if args.get("soak_s") else ""
            return f"exchange{d} {args.get('washes', 1)} x {args['volume_ul']:g} uL {args.get('reagent')} at {args['flow_ul_min']:g} uL/min{soak}"
        if command in ("load", "release"):
            return f"{command} {args.get('volume_ul', 0):g} uL at {args.get('flow_ul_min', 0):g} uL/min"
        if command == "hold_temp":
            return f"hold {args['temp_c']:g} C for {args['duration_s']:g} s (ramp {args.get('ramp_c_per_s', 1):g} C/s)"
        if command == "culture":
            return (f"culture {', '.join(args.get('targets', []))} at {args['temp_c']:g} C for "
                    f"{args['duration_s']:g} s, {args['volume_ul']:g} uL of medium every "
                    f"{args['exchange_interval_s']:g} s at {args['flow_ul_min']:g} uL/min")
        if command == "collect":
            return f"collect {args['volume_ul']:g} uL from {', '.join(args.get('targets', []))}"
        if command == "harvest":
            return f"harvest {', '.join(args.get('targets', []))} with {args.get('reagent')}"
        if command == "split":
            return f"split {args.get('source')} into {', '.join(args.get('targets', []))}"
        if command == "pool":
            return f"pool {', '.join(args.get('sources', []))} into {args.get('target')}"
        return command

    def _true(self) -> ChipState | None:
        t = self.adapter.true_state()
        return ChipState.from_dict(t) if t else None

    def rationale(self, plan: dict) -> str:
        """Why the optimizer chose this plan, in one sentence."""
        p, n = plan["predicted"], len(plan["candidates"])
        rej = sum(1 for r in plan["candidates"] if r["status"] == "rejected")
        bits = [f"{p['time_s']:.0f} s"]
        if p.get("final_pct") is not None:
            bits.append(f"{p['final_pct']:.1f}% exchanged")
        if p.get("peak_kpa"):
            bits.append(f"peak {p['peak_kpa']:.2f} kPa")
        return (f"Fastest safe option of {n} simulated ({rej} rejected): {self._describe(plan['command'], plan['args'])}, "
                f"predicted {', '.join(bits)}.")

    def _run(self, plan: dict, step_no: int, narration: str, verdict, recovery: bool = False, explanation: str = "") -> dict:
        command, args = plan["command"], dict(plan["args"])
        target = args.get("target")
        self._explanation = explanation or narration
        seg = self._new_segment(command, args, step_no, narration, verdict, recovery, plan)
        replans = 0
        if command in ("exchange", "load", "release"):
            per_wash = float(args["volume_ul"])
            remaining = per_wash * int(args.get("washes", 1))
            slice_v = per_wash / self.slices_per_wash
            done_in_wash = 0.0
            while remaining > 1e-9:
                v = min(slice_v, remaining)
                sl = {k: val for k, val in args.items() if k not in ("washes", "soak_s")}
                sl["volume_ul"] = v
                if command == "load":
                    sl["capsules"] = args.get("capsules", 0) * v / (per_wash * int(args.get("washes", 1)))
                wash_start = done_in_wash < 1e-9
                upd, alerts = self._slice(seg, command, sl, target, wash_start=wash_start)
                remaining -= v
                done_in_wash = (done_in_wash + v) % per_wash if per_wash else 0.0
                severe = [a for a in alerts if a["severity"] == "critical"]
                if severe:
                    self._close_segment(seg)
                    return {"status": "interrupted", "alerts": severe}
                if upd and remaining > 1e-9 and replans < 3 and command == "exchange" and not recovery:
                    replans += 1
                    new = self._replan(step_no, plan)
                    if new is None:
                        self._close_segment(seg)
                        return {"status": "no_safe_plan"}
                    if new is not False:
                        self._close_segment(seg)
                        plan, args = new, dict(new["args"])
                        per_wash = float(args["volume_ul"])
                        remaining = per_wash * int(args.get("washes", 1))
                        slice_v = per_wash / self.slices_per_wash
                        done_in_wash = 0.0
                        self._explanation = "Re-planned after a model update: " + self.rationale(plan)
                        seg = self._new_segment(command, args, step_no, narration + " (re-planned)", verdict, recovery, plan)
            if args.get("soak_s"):
                for _ in range(2):
                    self._slice(seg, "wait", {"duration_s": args["soak_s"] / 2}, target)
        elif command == "hold_temp":
            ramp_time = abs(args["temp_c"] - self.believed.chambers[target].temp_c) / args.get("ramp_c_per_s", 1.0)
            total = ramp_time + args["duration_s"]
            n = 6
            for _ in range(n):
                _, alerts = self._slice(seg, "hold_temp", {**args, "duration_s": total / n}, target)
                if any(a["severity"] == "critical" for a in alerts):
                    self._close_segment(seg)
                    return {"status": "interrupted", "alerts": alerts}
        else:
            self._slice(seg, command, args, target)
        self._close_segment(seg)
        return {"status": "done"}

    def _new_segment(self, command, args, step_no, narration, verdict, recovery, plan) -> dict:
        return {"t_s": round(self.believed.clock_s, 3), "command": command, "plan_args": dict(args),
                "protocol_step": step_no, "narration": narration, "operator": self.operator,
                "explanation": getattr(self, "_explanation", narration),
                "safety": verdict.to_dict(), "recovery": recovery, "slices": [], "model_updates": [], "alerts": [],
                "volume_done": 0.0, "plan_step": plan.get("step")}

    def _close_segment(self, seg: dict) -> None:
        if not seg["slices"]:
            return
        command, a = seg["command"], seg["plan_args"]
        args = {k: a[k] for k in EVENT_ARG_KEYS.get(command, ()) if k in a}
        if command == "exchange":
            per = float(a["volume_ul"])
            washes = max(1, round(seg["volume_done"] / per)) if per else 1
            args.update(volume_ul=round(seg["volume_done"] / washes, 3), washes=int(washes))
        elif command == "load":
            args["volume_ul"] = round(seg["volume_done"], 3)
        elif command == "hold_temp":
            args["duration_s"] = a["duration_s"]
        event = {"t_s": seg["t_s"], "duration_s": round(self.believed.clock_s - seg["t_s"], 3), "command": command,
                 "args": args, "narration": seg["narration"], "protocol_step": seg["protocol_step"],
                 "operator": seg["operator"], "explanation": seg["explanation"], "safety": seg["safety"],
                 "recovery": seg["recovery"], "plan_args": seg["plan_args"], "slices": seg["slices"],
                 "model_updates": seg["model_updates"], "alerts": seg["alerts"]}
        self.events.append(event)

    def _slice(self, seg: dict, command: str, sl: dict, target: str, wash_start: bool = False) -> tuple[list, list]:
        before = self.believed
        if command == "exchange":
            a = self._anchor
            if not a or a["target"] != target or a["reagent"] != sl["reagent"]:
                ch = before.chambers[target]
                c0 = ch.concentration if ch.reagent == sl["reagent"] else 0.0
                self._anchor = {"target": target, "reagent": sl["reagent"], "c0": c0, "volume": 0.0}
            self._anchor["volume"] += sl["volume_ul"]
        pred_state, pred = self.model.predict(before, command, sl)
        self.adapter.send(command, sl, wash_start=wash_start)
        readings = self.adapter.read_sensors()
        predicted_readings = readings if self.readings_are_predictions else {}
        meas = {} if self.readings_are_predictions else {k: v for k, v in readings.items() if k in self.trusted}
        self.last_measurement = meas
        true = self._true()
        believed = self._fuse(pred_state, pred, meas, command, sl, target, before)
        errors = self._errors(pred, meas, before, target)
        self.believed = believed

        # The monitor runs BEFORE the model is refitted. It used to run after, so a stuck or
        # wild sensor had already been fitted into mesh_resistance by the time the
        # disagreement was raised, and nothing ever took that back.
        alerts = self.monitor.check(self, command, sl, pred, meas, target) if self.monitor else []
        for a in alerts:
            seg["alerts"].append(a)
        doubted = {a["evidence"].get("sensor") for a in alerts if a["name"] == "sensor_disagreement"}
        fit_meas = {k: v for k, v in meas.items() if k not in doubted and k in self.trusted}
        fit_errors = {k: v for k, v in errors.items() if k not in doubted}
        updates = self._update_model(command, sl, pred, fit_meas, before, target, fit_errors)
        if updates:  # re-predict this slice's end state with the corrected model, keeping measurements
            for u in updates:
                seg["model_updates"].append(u)
        if command in ("exchange", "load", "release"):
            seg["volume_done"] += sl["volume_ul"]
        t_chamber = true.chambers[target] if (true and target in true.chambers) else None
        seg["slices"].append({
            "t_s": round(self.believed.clock_s, 3), "dt_s": round(pred["dt_s"], 3), "command": command,
            "flow_ul_min": sl.get("flow_ul_min"), "volume_ul": round(sl.get("volume_ul", 0.0), 4),
            "predicted": {"pressure_kpa": round(pred["pressure_kpa"], 4), "outlet_absorbance": _r(pred["outlet_absorbance"]),
                          "temp_c": _r(pred["temp_c"])},
            # "measured" holds readings from instruments only. An adapter that answers with
            # the model's own predictions (the dry run) gets its own, clearly named field:
            # printing those under "measured" was the model agreeing with itself.
            "measured": {k: v for k, v in meas.items()},
            "readings_source": "model_prediction" if self.readings_are_predictions else "sensors",
            **({"model_readings": dict(predicted_readings)} if predicted_readings else {}),
            "true": None if t_chamber is None else {"pressure_kpa": round(t_chamber.pressure_kpa, 4),
                                                    "outlet_absorbance": round(t_chamber.concentration, 4),
                                                    "temp_c": round(t_chamber.temp_c, 3)},
            "error": errors,
            "believed": self.believed.summary(target),
            "true_state": true.summary(target) if true else None,
        })
        return updates, alerts

    def _fuse(self, s: ChipState, pred: dict, meas: dict, command: str, sl: dict, target: str, before: ChipState) -> ChipState:
        """Correct the model's predicted state with whatever trusted sensors measured."""
        if not meas or target not in s.chambers:
            return s
        ch = s.chambers[target]
        if "pressure_kpa" in meas and ch.has_retention and command in ("exchange", "load"):
            ch.pressure_kpa = meas["pressure_kpa"]
        if meas.get("outlet_absorbance") is not None and command in ("exchange", "load", "release"):
            ch.concentration = min(1.0, max(0.0, meas["outlet_absorbance"]))
        if "temperature_c" in meas:
            ch.temp_c = meas["temperature_c"]
        if "pump_volume_ul" in meas and pred["dispensed_ul"] > 0:
            delta = pred["dispensed_ul"] - meas["pump_volume_ul"]
            reagent = sl.get("reagent") or (CARRIER if command == "load" else None)
            if reagent in s.reservoirs_ul:
                s.reservoirs_ul[reagent] += delta
            if command == "release":
                s.collection_ul -= delta
            else:
                s.waste_ul -= delta
        if "capsules_counted" in meas:
            # A counter correction can only MOVE capsules between compartments: it cannot
            # make them, and it cannot take out more than a compartment holds. Unclamped,
            # one noisy count pushed capsules_lost negative or collected more capsules than
            # were ever loaded, and the recovery percentage in the report was then
            # arithmetic on an impossible chip.
            counted = meas["capsules_counted"]
            if command == "release":
                delta = counted - pred["capsules_out_collection"]
                delta = max(-s.capsules_collected, min(delta, ch.capsules_suspended))
                s.capsules_collected += delta
                ch.capsules_suspended -= delta
            elif command in ("exchange", "load"):
                extra = counted - pred["capsules_out_waste"]
                extra = max(-s.capsules_lost, min(extra, ch.capsules_on_mesh))
                s.capsules_lost += extra
                ch.capsules_on_mesh -= extra
        self._enforce_conservation(s, command)
        return s

    def _enforce_conservation(self, s: ChipState, command: str) -> None:
        """Keep the believed chip possible: loaded == on a mesh + suspended + collected + lost."""
        fixes = s.enforce_conservation()
        problem = s.conservation_problem()
        if fixes or problem:
            entry = {"t_s": round(s.clock_s, 2), "command": command, "fixes": fixes, "problem": problem}
            self.conservation_fixes.append(entry)
            message = ("capsule count would not add up after " + command
                       + (f": {problem}" if problem else "")
                       + (f"; corrected {', '.join(fixes)}" if fixes else ""))
            self._log("model_update", message, **entry)

    def _errors(self, pred: dict, meas: dict, before: ChipState, target: str) -> dict:
        err = {}
        if "pressure_kpa" in meas and abs(pred["pressure_kpa"]) > 0.2:
            err["pressure_kpa"] = round((meas["pressure_kpa"] - pred["pressure_kpa"]) / abs(pred["pressure_kpa"]), 4)
        if meas.get("outlet_absorbance") is not None and pred["outlet_absorbance"] is not None:
            # Compare with the model's prediction from the start of this exchange (evidence accumulates).
            a = self._anchor
            if a and a["target"] == target:
                expect = 1 - (1 - a["c0"]) * math.exp(-a["volume"] / self.model.params.chamber_volume_ul[target])
            else:
                expect = pred["outlet_absorbance"]
            rem_p = max(1 - expect, 0.02)
            err["outlet_absorbance"] = round(((1 - meas["outlet_absorbance"]) - (1 - expect)) / rem_p, 4)
        if "temperature_c" in meas and pred["temp_c"] is not None:
            err["temperature_c"] = round(meas["temperature_c"] - pred["temp_c"], 3)
        return err

    def _update_model(self, command, sl, pred, meas, before: ChipState, target, errors) -> list[dict]:
        """Simple estimation: refit mesh resistance from pressure and chamber volume from absorbance."""
        updates = []
        p = self.model.params
        if command == "exchange" and sl.get("direction") != "backflush" and "pressure_kpa" in errors \
                and abs(errors["pressure_kpa"]) > self.error_threshold:
            q = sl["flow_ul_min"]
            ch = before.chambers[target]
            cover = min(1.0, (ch.capsules_on_mesh + ch.capsules_suspended) / max(p.monolayer_capacity[target], 1.0))
            if cover > 0.2 and q > 0:
                fit = max(0.0, (meas["pressure_kpa"] / q - p.mesh_clean_resistance[target]) / cover)
                old = p.mesh_resistance[target]
                new = old + 0.8 * (fit - old)
                p.mesh_resistance[target] = new
                updates.append(self._model_update(f"mesh_resistance[{target}]", old, new, "kPa per uL/min",
                                                  f"pressure {meas['pressure_kpa']:.2f} kPa vs predicted {pred['pressure_kpa']:.2f} "
                                                  f"({100 * errors['pressure_kpa']:+.0f}%)",
                                                  sensor="pressure_kpa", attr="mesh_resistance", key=target))
        a = self._anchor
        if command == "exchange" and a and "outlet_absorbance" in errors and abs(errors["outlet_absorbance"]) > self.error_threshold:
            c0, c1, v = a["c0"], meas["outlet_absorbance"], a["volume"]
            if 0 <= c0 < 0.9 and c0 + 0.03 < c1 < 0.985 and v > 0:
                fit = -v / math.log((1 - c1) / (1 - c0))
                old = p.chamber_volume_ul[target]
                new = old + 0.7 * (fit - old)
                if 0.3 * old < new < 3 * old:
                    p.chamber_volume_ul[target] = new
                    updates.append(self._model_update(f"chamber_volume_ul[{target}]", old, new, "uL",
                                                      f"outlet absorbance {c1:.3f} after {v:.0f} uL: leftover old liquid "
                                                      f"{100 * errors['outlet_absorbance']:+.0f}% vs the model since the exchange began",
                                                      sensor="outlet_absorbance", attr="chamber_volume_ul", key=target))
        return updates

    def _model_update(self, param: str, old: float, new: float, unit: str, reason: str,
                      sensor: str | None = None, attr: str | None = None, key: str | None = None,
                      record_fit: bool = True) -> dict:
        u = {"t_s": round(self.believed.clock_s, 2), "param": param, "old": round(old, 5), "new": round(new, 5),
             "unit": unit, "reason": reason}
        if sensor:
            u["sensor"] = sensor
        self.model_updates.append(u)
        if record_fit and sensor and attr and key is not None:
            # Keep what this refit rested on, so distrusting that sensor can undo it.
            self.fits.append({"sensor": sensor, "attr": attr, "key": key, "old": old, "new": new, "unit": unit})
        self._log("model_update", f"{param}: {old:.4g} -> {new:.4g} {unit} ({reason})", **u)
        return u

    def _replan(self, step_no: int, plan: dict):
        """Re-plan the rest of a step after a model update. False = keep going; None = no safe plan."""
        goal = plan["goal"]
        new = optimizer.plan_step(goal, self.believed, self.model, self.chip, run_params=self.run_params,
                                  lyse_ahead=self._lyse_ahead(step_no, goal.get("args") or goal))
        if not new["feasible"]:
            self._log("blocked", f"step {step_no}: after the model update no safe plan meets the goal", step_no)
            return None
        old_a, new_a = plan["args"], new["args"]
        same = (new_a.get("flow_ul_min") == old_a.get("flow_ul_min")
                and abs((new_a.get("volume_ul") or 0) * (new_a.get("washes") or 1)
                        - (old_a.get("volume_ul") or 0) * (old_a.get("washes") or 1)) < 1e-6)
        if same:
            return False
        new["replanned_from"] = {k: old_a.get(k) for k in ("flow_ul_min", "volume_ul", "washes")}
        self.plans[step_no] = new
        self._log("replan", f"step {step_no}: re-planned the rest after a model update: {self._describe(new['command'], new_a)}",
                  step_no, plan=new)
        return new

    # ------------------------------------------------------------------ results

    def _main_chamber(self) -> str:
        for c in self.chip["chambers"]:
            if c.get("retention") or c.get("mesh_pore_um"):
                return c["id"]
        return self.chip["chambers"][0]["id"]

    def _step_result(self, step_no: int) -> dict:
        step = self.steps[step_no]
        t = self._true()
        # A step names its chamber in "target" or in "targets" (culture, collect, harvest):
        # indexing "target" alone raised KeyError on every step of the second kind.
        target = step.get("target") or (list(step.get("targets") or []) or [self._main_chamber()])[0]
        out = {"believed": self.believed.summary(target)}
        if t:
            out["true"] = t.summary(target)
        return out

    def summary(self) -> dict:
        cid = self._main_chamber()
        t = self._true()
        b = self.believed
        wash_steps = [n for n, s in self.steps.items() if s["operation"] == "exchange" and s.get("direction") != "backflush"]
        last_wash = max([n for n in wash_steps if n in self.step_results], default=None)
        counted = "capsules_counted" in self.trusted
        res = {"status": self.status, "status_reason": self.status_reason, "loop": self.loop,
               "duration_s": round(b.clock_s, 1), "alerts": len(self.alerts), "recoveries": self.recoveries,
               "model_updates": len(self.model_updates),
               "alert_names": sorted({a["name"] for a in self.alerts}),
               "trusted_sensors": sorted(self.trusted),
               # Where the believed column comes from, so a reader never has to guess
               # whether a number was measured. In an open-loop run nothing was.
               "believed_basis": ("model only: no sensor was on, so every believed number is a PREDICTION"
                                  if not self.trusted else
                                  "model corrected by " + ", ".join(sorted(self.trusted))),
               "capsule_recovery_basis": (
                   "ASSUMED: the denominator is the capsules the chip spec says were loaded "
                   "(capsule.count - they were never counted), and the numerator is "
                   + ("the outlet counter's count" if counted else
                      "the MODEL's prediction of how many came out (no capsule counter was on)")),
               "conservation": {"checks_failed": len(self.conservation_fixes),
                                "believed_unaccounted": round(b.conservation_error(), 2),
                                "invariant": "capsules_loaded == on a mesh + suspended + collected + lost"}}

        def side(state: ChipState | None) -> dict | None:
            if state is None:
                return None
            loaded = max(state.capsules_loaded, 1e-9)
            used = {r: round(self.initial_reservoirs.get(r, 0) - v, 2) for r, v in state.reservoirs_ul.items()
                    if r in self.initial_reservoirs}
            return {"capsule_recovery_pct": round(100 * state.capsules_collected / loaded, 2) if state.capsules_loaded else None,
                    "capsule_recovery_is_assumed": state.label != "true",
                    "capsules_collected": round(state.capsules_collected), "capsules_lost": round(state.capsules_lost),
                    "capsules_on_mesh": round(state.chambers[cid].capsules_on_mesh),
                    "capsules_loaded": round(state.capsules_loaded),
                    "capsules_unaccounted": round(state.conservation_error(), 2),
                    "reagent_used_ul": used, "waste_ul": round(state.waste_ul, 2), "collection_ul": round(state.collection_ul, 2)}

        res["believed"] = side(b)
        res["true"] = side(t)
        if last_wash is not None:
            sr = self.step_results[last_wash]
            res["final_exchange_pct"] = {"step": last_wash, "believed": sr["believed"]["exchange_pct"],
                                         "true": sr.get("true", {}).get("exchange_pct")}
        if t is not None:
            tp = self.adapter.true_params() if hasattr(self.adapter, "true_params") else {}
            drift = {
                "capsules_collected": abs(res["believed"]["capsules_collected"] - res["true"]["capsules_collected"]),
                "capsules_lost": abs(res["believed"]["capsules_lost"] - res["true"]["capsules_lost"]),
                "waste_ul": round(abs(b.waste_ul - t.waste_ul), 2),
            }
            if "final_exchange_pct" in res and res["final_exchange_pct"]["true"] is not None:
                drift["final_exchange_pct_points"] = round(abs(res["final_exchange_pct"]["believed"] - res["final_exchange_pct"]["true"]), 2)
            for key in ("mesh_resistance", "chamber_volume_ul"):
                if key in tp and cid in tp[key]:
                    believed_v = getattr(self.model.params, key)[cid]
                    true_v = tp[key][cid]
                    initial_v = getattr(self.initial_model, key)[cid]
                    drift[f"{key}_error_pct"] = {"start": round(100 * (initial_v - true_v) / true_v, 1),
                                                 "end": round(100 * (believed_v - true_v) / true_v, 1)}
            res["drift"] = drift
        return res


def _r(v):
    return None if v is None else round(v, 4)
