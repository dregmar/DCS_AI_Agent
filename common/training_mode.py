"""
common/training_mode.py - one training mode (Andre, 1 Nov 2026).

Training targets (common/training.py + common/training_targets.py) and the instructor (instructor/)
stay separate modules: the training gate is still the only thing that can change the mission, and
the instructor still sends nothing. This only routes what the pilot says and joins the answers.
Every answer is the instructor's (Jenny's) voice.

    Training mode on / off                bare, or after "Overlord," / "Instructor,"; also
                                          "Instructor mode on / off", "Instructor, (mode) on / off"
    Overlord / Instructor, spawn ...      training targets (only while training mode is on)
    Overlord / Instructor, clear training targets
    Overlord / Instructor, training status
    Instructor, hover exercise / begin    the instructor itself (main.py), only while training is on

Training mode on works without the Game Master login, with Olympus off or with other players in the
mission: the exercises work and spawns are refused with the reason. Training mode off stops a
running exercise (stored as aborted, not scored) and removes the training targets.
"""
import re
from typing import Optional, Tuple

from common import overlord as overlord_mod
from common.training import VOICE
from instructor import voice as instructor_voice

# A bare switch is the whole utterance: "training mode on", "start training", "end the training".
_MAX_BARE_WORDS = 6
# "Instructor mode on / off" (said bare or after "Instructor,"), "Instructor, on / off / mode on / off"
_INSTR_MODE = re.compile(r"^(?:instructor )?(?:mode )?(on|off)$")
_BARE_INSTR = re.compile(r"^\s*instructors? mode (on|off)\s*[.!]*\s*$", re.I)


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[.,!?;:]", " ", (t or "").lower())).strip()


class TrainingMode:
    def __init__(self, training, spawner, instructor=None):
        self.training, self.spawner, self.instructor = training, spawner, instructor

    def wanted(self, text: str) -> Optional[Tuple[str, str]]:
        """(action, rest) for a training-mode utterance, else None. action: on / off / clear / status / spawn."""
        m = _BARE_INSTR.match(text or "")
        if m:
            return m.group(1).lower(), ""
        rest = overlord_mod.addressed(text)
        if rest is None:
            rest = instructor_voice.addressed(text)
            if rest is not None:
                m = _INSTR_MODE.match(_clean(rest))
                if m:
                    return m.group(1), rest
        if rest is not None:
            act = self.training.action(rest)
            if act is not None:
                return act, rest
            if self.spawner is not None and self.spawner.wanted(rest):
                return "spawn", rest
            return None
        # bare: only the on / off switch, and only when it is (nearly) the whole utterance
        t = _clean(text)
        if not t or len(t.split()) > _MAX_BARE_WORDS:
            return None
        act = self.training.action(t)
        return (act, t) if act in ("on", "off") else None

    def handle(self, action: str, rest: str = "") -> str:
        if action == "on":
            return self.training.set_mode(True)
        if action == "off":
            first = ""
            if self.instructor is not None:
                first = self.instructor.stand_down("training mode off")
            said = self.training.set_mode(False)
            if first and said.startswith(VOICE):          # one "Instructor," at the front
                said = said[len(VOICE):]
                said = said[0].upper() + said[1:]
            return f"{first} {said}".strip()
        if action == "clear":
            return self.training.say_clear()
        if action == "status":
            said = self.training.status_text()
            if self.instructor is not None and self.training.on:
                more = self.instructor.status_line()
                if more:
                    said += " " + more
            return said
        if action == "spawn":
            return self.spawner.handle(rest)
        return VOICE + "say again."
