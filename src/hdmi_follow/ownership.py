"""Pure debounced ownership state, independent of USB and the network."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Transition:
    present: bool
    generation: int


class OwnershipTracker:
    def __init__(self, debounce: float = 1, resume_gap: float = 5) -> None:
        self.debounce = debounce
        self.resume_gap = resume_gap
        self.stable: bool | None = None
        self.candidate: bool | None = None
        self.since = 0.0
        self.last: float | None = None
        self.generation = 0

    def update(self, present: bool | None, now: float) -> Transition | None:
        gap = self.last is not None and not 0 <= now - self.last <= self.resume_gap
        self.last = now
        if present is None or gap:
            self.stable = None
            self.candidate = None
            self.generation += 1
            return None
        if self.candidate != present:
            self.candidate = present
            self.since = now
            # Invalidate an in-flight command as soon as ownership becomes uncertain.
            self.generation += 1
        if now - self.since < self.debounce:
            return None
        if self.stable is None:
            self.stable = present  # Establish a silent baseline, including after resume.
            return None
        if self.stable == present:
            return None
        self.stable = present
        return Transition(present, self.generation)

    def current(self, transition: Transition) -> bool:
        return (
            self.generation == transition.generation
            and self.candidate == transition.present
            and self.stable == transition.present
        )
