"use client";

import { Fragment, useRef } from "react";
import { useAuthSession } from "../lib/useAuthSession";

export default function AuthSessionBoundary({ children }) {
  const session = useAuthSession();
  const owner = useRef({ session, generation: 0 });
  if (owner.current.session !== session) {
    owner.current = { session, generation: owner.current.generation + 1 };
  }
  // Remount pages, not merely their DOM wrapper: old drafts/dialogs belong to
  // the previous session. No token is rendered as a DOM attribute or key.
  return <Fragment key={owner.current.generation}>{children}</Fragment>;
}
