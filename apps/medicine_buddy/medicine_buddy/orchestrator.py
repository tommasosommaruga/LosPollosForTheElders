"""Dose state machine wiring buddy, vision, arm, and adherence ports."""

from __future__ import annotations

from medicine_buddy.config import AppConfig
from medicine_buddy.db import Database
from medicine_buddy.ports import AdherencePort, ArmPort, BuddyPort, VisionPort
from medicine_buddy.states import DoseState


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
    ) -> DoseState:
        medicine = self.db.get_medicine(medicine_id)
        if medicine is None:
            raise ValueError(f"Unknown medicine_id={medicine_id}")

        name = medicine.name
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

        # Conversation-app path: pause here so Reachy TTS asks adherence itself.
        if stop_after_placed:
            return DoseState.PLACED

        self.db.record_transition(
            medicine_id,
            DoseState.WATCHING,
            slot_id=match.slot_id,
            detail="adherence watch started",
        )

        result = self.adherence.watch_or_ask(
            name, self.config.adherence_timeout_seconds
        )
        if result.taken:
            self.buddy.speak("Thank you. Dose marked as taken.")
            self.db.record_transition(
                medicine_id,
                DoseState.TAKEN,
                slot_id=match.slot_id,
                detail=f"{result.via}: {result.detail}",
            )
            return DoseState.TAKEN

        self.buddy.speak("I logged this dose as missed.")
        self.db.record_transition(
            medicine_id,
            DoseState.MISSED,
            slot_id=match.slot_id,
            detail=f"{result.via}: {result.detail}",
        )
        return DoseState.MISSED

    def finish_adherence(self, medicine_id: int, *, taken: bool) -> DoseState:
        """Complete a dose that stopped after Placed (conversation owns the ask)."""
        medicine = self.db.get_medicine(medicine_id)
        if medicine is None:
            raise ValueError(f"Unknown medicine_id={medicine_id}")

        current = self.db.latest_dose_state(medicine_id)
        if current != DoseState.PLACED:
            raise ValueError(
                f"Adherence requires state Placed, found {current}"
            )

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
