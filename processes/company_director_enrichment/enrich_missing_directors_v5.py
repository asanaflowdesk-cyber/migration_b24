from __future__ import annotations

import director_frozen_plan as frozen
import director_frozen_plan_hotfix as hotfix
import enrich_missing_directors_v4 as v4

# Подменяем только две точки исполнения apply:
# 1) targeted compatibility для уже утвержденного частично выполненного плана;
# 2) read-after-write verification с повторными чтениями.
frozen.load_frozen_plan = hotfix.load_frozen_plan_compatible
frozen.apply_frozen_plan = hotfix.apply_frozen_plan_reliable
v4.frozen = frozen


if __name__ == "__main__":
    raise SystemExit(v4.main())
