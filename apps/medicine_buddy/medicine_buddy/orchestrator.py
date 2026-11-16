"""Dose state machine wiring buddy, vision, arm, and adherence ports."""

from __future__ import annotations

from medicine_buddy.arm_protocol import DEFAULT_DESTINATION, normalize_destination
from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.http_arm import HttpArmClient
from medicine_buddy.ports import AdherencePort, ArmPort, BuddyPort, VisionPort
from medicine_buddy.states import TERMINAL_STATES, DoseState


class DoseStateMachine:
    def __init__(
        self,
        db: Database,
        buddy: BuddyPort,
        vision: VisionPort,
        arm: ArmPort,
        adherence: AdherencePort,
        config: AppConfig | None = None,
    ) -> None:
        self.db = db
        self.buddy = buddy
        self.vision = vision
        self.arm = arm
        self.adherence = adherence
        self.config = config or AppConfig()

    def run_dose(
        self,
        medicine_id: int,
        *,
        stop_after_placed: bool = False,
        person_name: str | None = None,
        destination: str = DEFAULT_DESTINATION,
    ) -> DoseState:
        medicine = self.db.get_medicine(medicine_id)
        if medicine is None:
            raise ValueError(f"Unknown medicine_id={medicine_id}")

        name = medicine.name
        dest = normalize_destination(destination)
        self.db.record_transition(
            medicine_id, DoseState.DUE, detail="dose due", from_state=DoseState.IDLE
        )

        self.buddy.speak(f"Time for your {name} medicine!")
        self.db.record_transition(medicine_id, DoseState.REMINDED, detail="reminder spoken")

        confirmed = self.buddy.ask_yes_no(
            f"Time for your {name} — should I bring it?"
        )
        if not confirmed:
            self.buddy.speak("Okay, I will skip this dose for now.")
            self.db.record_transition(
                medicine_id, DoseState.SKIPPED, detail="person declined"
            )
            return DoseState.SKIPPED

        self.db.record_transition(
            medicine_id, DoseState.CONFIRMED, detail="person confirmed"
        )

        # Remote arm PC: structured HTTP JSON after confirm (one-shot locate/pick/place).
        if isinstance(self.arm, HttpArmClient):
            return self._run_remote_arm(
                medicine_id,
                name,
                person_name=person_name,
                stop_after_placed=stop_after_placed,
                destination=dest,
            )

        return self._run_local_ports(
            medicine_id, name, stop_after_placed=stop_after_placed,
        )

    def _run_remote_arm(
        self,
        medicine_id: int,
        name: str,
        *,
        person_name: str | None,
        stop_after_placed: bool,
        destination: str = DEFAULT_DESTINATION,
    ) -> DoseState:
        assert isinstance(self.arm, HttpArmClient)
        dest = normalize_destination(destination)
        # Reachy should say this aloud (via speak_hint) while the JSON POST is in flight.
        self.buddy.speak(
            f"Okay — I'm sending a request to the arm to bring your {name} now."
        )
        self.db.record_transition(
            medicine_id, DoseState.LOCATING, detail="arm.v1 dispense signal sent"
        )
        self.db.record_transition(
            medicine_id, DoseState.PICKING, detail="waiting for arm one-shot detect"
        )
        try:
            result = self.arm.dispense(
                medicine_id=medicine_id,
                medicine_name=name,
                person=person_name,
                destination=dest,
            )
        except Exception as exc:  # noqa: BLE001
            self.buddy.speak(
                "I could not reach the arm computer to send that package. "
                "Please check the arm link."
            )
            self.db.record_transition(
                medicine_id, DoseState.FAILED, detail=f"arm HTTP error: {exc}",
            )
            return DoseState.FAILED

        if not result.ok or result.final_state != "placed":
            detail = result.error or result.detect_detail or result.final_state
            self.buddy.speak(
                f"The arm got my request, but could not bring your {name}. "
                f"{detail or 'Please check the tray.'}"
            )
            self.db.record_transition(
                medicine_id,
                DoseState.FAILED,
                slot_id=result.slot_id,
                detail=f"arm dispense_result: {detail}",
            )
            return DoseState.FAILED

        self.db.record_transition(
            medicine_id,
            DoseState.PLACED,
            slot_id=result.slot_id,
            detail=result.detect_detail or f"label={result.label} score={result.score}",
        )
        self.buddy.speak(
            f"The arm placed your {name} at the {dest}. Please take it."
        )

        if stop_after_placed:
            return DoseState.PLACED

        return self._watch_after_place(
            medicine_id, name, slot_id=result.slot_id, person_name=person_name,
        )

    def _run_local_ports(
        self, medicine_id: int, name: str, *, stop_after_placed: bool,
    ) -> DoseState:
        self.db.record_transition(
            medicine_id, DoseState.LOCATING, detail="OCR locate started"
        )
        match = self.vision.locate_medicine(name)
        if match is None:
            self.buddy.speak(
                f"I could not confidently find {name} on the tray. I will not move the arm."
            )
            self.db.record_transition(
                medicine_id,
                DoseState.FAILED,
                detail="OCR miss or low confidence",
            )
            return DoseState.FAILED

        if match.score < self.config.ocr_min_confidence:
            self.buddy.speak(
                f"I am not sure enough about {name} (score {match.score:.2f}). Aborting."
            )
            self.db.record_transition(
                medicine_id,
                DoseState.FAILED,
                slot_id=match.slot_id,
                detail=f"OCR score {match.score:.2f} below threshold",
            )
            return DoseState.FAILED

        self.db.record_transition(
            medicine_id,
            DoseState.PICKING,
            slot_id=match.slot_id,
            detail=f"matched {match.label} score={match.score:.2f}",
        )
        try:
            self.arm.replay_slot_to_bowl(match.slot_id)
        except Exception as exc:  # noqa: BLE001 — surface hardware faults as Failed
            if self.config.dry_run:
                self.buddy.speak("The simulated arm had a software problem, so nothing was moved.")
            else:
                self.buddy.speak("The arm failed while moving. Please check the e-stop.")
            self.db.record_transition(
                medicine_id,
                DoseState.FAILED,
                slot_id=match.slot_id,
                detail=f"arm error: {exc}",
            )
            return DoseState.FAILED

        place = self.arm.verify_place(match.slot_id)
        if not place.ok:
            self.buddy.speak("I could not verify the medicine reached the bowl.")
            self.db.record_transition(
                medicine_id,
                DoseState.FAILED,
                slot_id=match.slot_id,
                detail=place.detail,
            )
            return DoseState.FAILED

        self.db.record_transition(
            medicine_id,
            DoseState.PLACED,
            slot_id=match.slot_id,
            detail=place.detail,
        )
        self.buddy.speak(f"Your {name} is in the bowl. Please take it.")

        if stop_after_placed:
            return DoseState.PLACED

        return self._watch_after_place(medicine_id, name, slot_id=match.slot_id)

    def _watch_after_place(
        self,
        medicine_id: int,
        name: str,
        *,
        slot_id: int | None,
        person_name: str | None = None,
    ) -> DoseState:
        self.db.record_transition(
            medicine_id,
            DoseState.WATCHING,
            slot_id=slot_id,
            detail="adherence watch started",
        )

        # Prefer arm one-shot camera when remote client is wired.
        if isinstance(self.arm, HttpArmClient):
            try:
                check = self.arm.check_taken(
                    medicine_id=medicine_id,
                    medicine_name=name,
                    person=person_name,
                    slot_id=slot_id,
                    timeout_seconds=self.config.adherence_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                self.buddy.speak("I could not check the camera. Did you take it?")
                answered = self.buddy.ask_yes_no("Did you take it?")
                result_taken = answered
                detail = f"arm check_taken error, voice fallback: {exc}"
                via = "voice"
            else:
                if check.taken is None or not check.ok:
                    self.buddy.speak("I'm not sure from the camera. Did you take it?")
                    answered = self.buddy.ask_yes_no("Did you take it?")
                    result_taken = answered
                    detail = f"camera inconclusive ({check.detail}); voice={'yes' if answered else 'no'}"
                    via = "voice"
                else:
                    adh = self.arm.to_adherence(check)
                    result_taken = adh.taken
                    detail = adh.detail
                    via = adh.via
        else:
            result = self.adherence.watch_or_ask(
                name, self.config.adherence_timeout_seconds
            )
            result_taken = result.taken
            detail = result.detail
            via = result.via

        if result_taken:
            self.buddy.speak("Thank you. Dose marked as taken.")
            self.db.record_transition(
                medicine_id,
                DoseState.TAKEN,
                slot_id=slot_id,
                detail=f"{via}: {detail}",
            )
            return DoseState.TAKEN

        self.buddy.speak("I logged this dose as missed.")
        self.db.record_transition(
            medicine_id,
            DoseState.MISSED,
            slot_id=slot_id,
            detail=f"{via}: {detail}",
        )
        return DoseState.MISSED

    def finish_adherence(self, medicine_id: int, *, taken: bool) -> DoseState:
        """Close a dose: after the arm placed it (Placed), or when the person simply reports
        that they took / missed it with no dispense (no arm connected, or they took it themselves)."""
        medicine = self.db.get_medicine(medicine_id)
        if medicine is None:
            raise ValueError(f"Unknown medicine_id={medicine_id}")

        current = self.db.latest_dose_state(medicine_id)
        final_state = DoseState.TAKEN if taken else DoseState.MISSED
        how = "taken" if taken else "missed"

        if current in (None, *TERMINAL_STATES):
            # No open dose: record one on the fly, then the person's report.
            self.db.record_transition(
                medicine_id, DoseState.DUE, detail="person reported without a reminder",
                from_state=DoseState.IDLE,
            )
            current = DoseState.DUE
        if current in (DoseState.DUE, DoseState.REMINDED):
            self.db.record_transition(
                medicine_id, final_state, detail=f"voice: person reported {how} (no dispense)"
            )
            self.buddy.speak("Thank you. Dose marked as taken." if taken else "I logged this dose as missed.")
            return final_state
        if current != DoseState.PLACED:
            raise ValueError(f"A dose is in progress ({current.value}); finish or cancel it first.")

        # Find last slot from events
        slot_id = None
        for event in reversed(self.db.list_dose_events(medicine_id)):
            if event.slot_id is not None:
                slot_id = event.slot_id
                break

        self.db.record_transition(
            medicine_id,
            DoseState.WATCHING,
            slot_id=slot_id,
            detail="adherence via conversation app",
        )
        if taken:
            self.buddy.speak("Thank you. Dose marked as taken.")
            self.db.record_transition(
                medicine_id,
                DoseState.TAKEN,
                slot_id=slot_id,
                detail="voice: conversation confirmed taken",
            )
            return DoseState.TAKEN

        self.buddy.speak("I logged this dose as missed.")
        self.db.record_transition(
            medicine_id,
            DoseState.MISSED,
            slot_id=slot_id,
            detail="voice: conversation confirmed missed",
        )
        return DoseState.MISSED
