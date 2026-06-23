// A non-independent dwelling ("onzelfstandige woonruimte") is a room with
// shared kitchen/bathroom — we only notify about self-contained homes. Studios
// and apartments are self-contained (zelfstandig) and kept. We inspect only the
// URL slug and the address/title, never the "X kamers" count, so a room count
// can never trigger a false positive. Street names like "Kamerlingh Onnesweg"
// are safe: the slug/word-boundary patterns require "kamer" to stand alone.
const ROOM_URL_PATTERN =
  /kamer-te-huur|studentenkamer|student-room|room-for-rent|\/kamers?(?:\/|$)|\/rooms?(?:\/|$)/i;
const ROOM_TITLE_PATTERN = /\bstudentenkamer\b|\bkamer\b|\broom\b/i;

export function isNonIndependentDwelling(huis: {
  url: string;
  straatnaamHuisnummer: string;
}): boolean {
  return (
    ROOM_URL_PATTERN.test(huis.url) ||
    ROOM_TITLE_PATTERN.test(huis.straatnaamHuisnummer)
  );
}
