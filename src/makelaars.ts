import { Makelaar, PlatformType } from "./makelaar";

export const makelaars: Makelaar[] = [
  {
    name: "van Daal Makelaardij",
    url: "https://vandaalmakelaardij.nl/",
    scrapeUrl:
      "https://www.vandaalmakelaardij.nl/nl/realtime-listings/consumer?pageKey=",
    platformType: PlatformType.RealtimeListingsJson,
  },
  {
    name: "Björnd Makelaardij",
    url: "https://bjornd.nl/",
    scrapeUrl: "https://www.bjornd.nl/nl/realtime-listings/consumer",
    platformType: PlatformType.RealtimeListingsJson,
  },
];
