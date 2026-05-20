export const DELFT_AREA_CITIES = [
  "Delft",
  "Delfgauw",
  "Den Hoorn",
  "Rijswijk",
  "Schipluiden",
  "Nootdorp",
  "Pijnacker",
] as const;

const CITY_PATTERNS = DELFT_AREA_CITIES.map(
  (city) => new RegExp(`\\b${city.toLowerCase()}\\b`),
);

// Accepts a bare city name or a longer string (e.g. "2611 AB Delft") so every
// response processor can share one filter regardless of how it extracts the city.
export function isDelftAreaCity(text: string): boolean {
  const lowered = text.toLowerCase();
  return CITY_PATTERNS.some((pattern) => pattern.test(lowered));
}
