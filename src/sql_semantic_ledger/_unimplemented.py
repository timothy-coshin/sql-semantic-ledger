"""Uniform handling of features that are planned but not implemented yet."""
from __future__ import annotations

from typing import NoReturn

from .features import BY_ID


class NotImplementedYet(NotImplementedError):
    """Raised by planned features. The message names the feature and the plan stage."""


def planned(feature_id: str) -> NoReturn:
    """Raise :class:`NotImplementedYet` for a planned feature listed in :mod:`features`.

    For a ``partial`` feature the stub stands for its missing part, so the message says what this release
    already does (the feature's note) instead of calling the whole feature unimplemented.
    """
    f = BY_ID.get(feature_id)
    if f is None:
        raise NotImplementedYet(f'{feature_id}: not implemented yet')
    if f.status == 'partial':
        raise NotImplementedYet(f'{f.name_en} [{f.id}] is only partly implemented in this release ({f.note}); the '
                                f'missing part is planned for {f.stage_en} of the research roadmap.')
    raise NotImplementedYet(f'{f.name_en} [{f.id}] is planned for {f.stage_en} of the research roadmap and is not '
                            f'implemented in this release.')
