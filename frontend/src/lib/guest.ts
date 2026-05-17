// no-login guest identity. on first visit we mint a UUID v4 and stash
// it in localStorage; every API call includes it as X-Guest-Id. the
// server uses it to scope the sidebar listing so each visitor sees
// only the runs they created. direct run URLs (capability links)
// still work for anyone — see pipeline/code/api.py:get_run.
//
// trade-offs (intentional for the research-preview phase):
//   - history does NOT follow a user across browsers / devices /
//     incognito sessions. one localStorage = one identity.
//   - clearing site data wipes history.
//   - the header is client-controlled, so it's not an auth boundary.
//     when real sign-in lands, runs migrate to outputs/users/<id>/
//     and the server stops trusting this header for ownership.

const KEY = "ploverai_guest_id";

export function getGuestId(): string {
  // SSR / build-time render: no window, no localStorage. return empty;
  // any caller that needs to authenticate against the API runs in the
  // browser (the components doing fetches are "use client").
  if (typeof window === "undefined") return "";
  let id = window.localStorage.getItem(KEY);
  if (!id) {
    // crypto.randomUUID is available in all evergreen browsers and
    // gives ~122 bits of entropy from a CSPRNG. enough that another
    // visitor can't guess your guest id by mistake.
    id = window.crypto.randomUUID();
    window.localStorage.setItem(KEY, id);
  }
  return id;
}
