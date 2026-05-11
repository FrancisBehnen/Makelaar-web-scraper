import { Makelaar, PlatformType } from "./makelaar";

export const makelaars: Makelaar[] = [
  {
    name: "van Silfhout & Hogetoorn",
    url: "https://www.vansilfhout.nl/",
    scrapeUrl:
      "https://www.vansilfhout.nl/woningaanbod/?fwp_status=te-huur&fwp_locaties=delfgauw%2Cdelft%2Cden-hoorn%2Crijswijk&fwp_huurprijs=0%2C1500.00",
    platformType: PlatformType.VanSilfhoutEnHogetoorn,
  },
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
  {
    name: "Verra Makelaars",
    url: "https://www.verra.nl/",
    scrapeUrl: "https://www.verra.nl/nl/realtime-listings/consumer",
    platformType: PlatformType.RealtimeListingsJson,
  },
];
