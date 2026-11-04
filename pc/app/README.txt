PC gesture task candidate — offline software delivery

Start manually: py -3.11 EMG_Studio/EMG_Studio/start_gesture_tasks.py
Dependencies: EMG_Studio/EMG_Studio/requirements.txt
Read docs/PC_GESTURE_TASKS_CUSTOMER_UI_20261101.md first.
Select training actions, set their hand responses, train, upload and verify, then explicitly enable.
Automatic upload after training is optional and initially disabled. It never automatically arms the hand.
Diagnostics log: EMG_Studio/EMG_Studio/runtime/gesture_tasks.log (relative to this package).
No automatic serial connection, flashing or servo action at startup.
A/B wire interfaces and firmware are unchanged. No A/B BIT is included.
A full-rate raw collection is implemented. Existing B pose/live-feedback/range limits remain.
Verification evidence uses mocks/synthetic data and hidden Tk windows. No hardware sign-off.

Raw stream background: docs/PC_GESTURE_TASKS_RAW43_20261101.md.
A compact raw 0x43 collection and model CRC identity query are implemented.
Existing B pose/live-feedback/range boundaries are documented; this candidate does not change interfaces.
Offline compact capture/training evidence and a copied synthetic A byte fixture are included.

DEFAULT MODEL FIX: read docs/PC_GESTURE_TASKS_A_READONLY_MATERIALS_20261101.md.
The prepared.npz and verified integer export are included. No retraining required.
Use the A-only identity check to show training actions without deploying to B.
START_PC_GESTURE_TASKS.cmd selects the correct working directory.

RECOGNITION SOURCE: docs/PC_GESTURE_TASKS_RECOGNITION_SOURCE_20261101.md.
Choose A or PC on the real-time recognition page, then explicitly start.
PC inference uses the current model and full-rate raw samples from A; it needs no B deployment.
PC output is a labeled preview. Mechanical control continues on A/B.

RECOVERED CURRENT MODEL: use START_RECOVERED_MODEL.cmd for the preserved 7-class session.
Read docs/PC_GESTURE_TASKS_MODEL_CHECK_FIX_20261101.md and docs/PC_GESTURE_TASKS_B_SYNC_FIX_20261101.md when included.
No retraining, automatic upload, board connection or mechanical action is performed.
