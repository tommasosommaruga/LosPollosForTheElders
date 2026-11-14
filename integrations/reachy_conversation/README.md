# Reachy Mini conversation integration (moved)

The conversation tools and the `medicine_caregiver` profile now ship **inside the Reachy Mini app**:

- tools: [`apps/medicine_buddy/medicine_buddy/conversation/external_tools/`](../../apps/medicine_buddy/medicine_buddy/conversation/external_tools/)
- profile: [`apps/medicine_buddy/medicine_buddy/conversation/external_profiles/medicine_caregiver/profile.md`](../../apps/medicine_buddy/medicine_buddy/conversation/external_profiles/medicine_caregiver/profile.md)
- app (starts the conversation app with them, the API and the voice guard): [`apps/medicine_buddy/medicine_buddy/main.py`](../../apps/medicine_buddy/medicine_buddy/main.py)

The old two-terminal setup (`run_conversation_with_medicine.sh`, `.env.generated`, `run_conversation.py`)
is gone. See [README.md → How to run](../../README.md#how-to-run):

```bash
python -m medicine_buddy.main --host <robot-ip>   # from a computer
```

or install it on the robot and start **Medicine Buddy** in Reachy Mini Control → Applications.
