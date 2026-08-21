import { useEffect, useState } from "react";

import { fetchSkills } from "@/lib/api";
import type { SkillsPayload } from "@/lib/types";

const EMPTY_SKILLS_PAYLOAD: SkillsPayload = { skills: [], installed_tools: [] };

export function useSkills(token: string): SkillsPayload {
  const [payload, setPayload] = useState<SkillsPayload>(EMPTY_SKILLS_PAYLOAD);

  useEffect(() => {
    let cancelled = false;
    fetchSkills(token)
      .then((nextPayload) => !cancelled && setPayload(nextPayload))
      .catch(() => !cancelled && setPayload(EMPTY_SKILLS_PAYLOAD));
    return () => {
      cancelled = true;
    };
  }, [token]);

  return payload;
}
