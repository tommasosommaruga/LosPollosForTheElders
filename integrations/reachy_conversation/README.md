# Reachy Mini conversation integration

**Robot host PC** (Reachy connected here): pull this repo and run.

```bash
git pull
./scripts/bootstrap.sh          # once
./scripts/run_robot_host.sh     # terminal 1 — Medicine Buddy API
./scripts/run_conversation_with_medicine.sh   # terminal 2 — pollen TTS app
```

`run_robot_host.sh` writes absolute paths into
`integrations/reachy_conversation/.env.generated` so conversation tools always
hit this checkout’s medicine API on `http://127.0.0.1:8787`.

```text
[Robot host PC — after git pull]
  medicine-buddy serve :8787
  reachy_mini_conversation_app  (TTS/STT) + our external tools
```

If Maker Arm later lives on a *second* PC, change `MEDICINE_BUDDY_URL` in
`.env.generated` to that PC’s LAN URL and run `medicine-buddy serve` there instead.

## Voice flow

1. “I take Aspirin every day at 9” → `schedule_medicine`
2. “Do I have medicine due?” → `check_due_medicines`
3. Reachy asks to bring it → yes → `dispense_medicine` (`confirmed=true`)
4. “Did you take it?” → `mark_dose_taken`

TTS always comes from the conversation app.
