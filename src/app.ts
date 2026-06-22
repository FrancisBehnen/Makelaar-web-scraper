import axios from "axios";
import * as fs from "node:fs";
import { Makelaar } from "./makelaar";
import { IHuis } from "./Huis";
import { IDatabase, SQLiteDatabase } from "./database";
import domProcessors from "./response-processors";
import { makelaars } from "./makelaars";
import { isNonIndependentDwelling } from "./dwellingFilter";

const USE_DEBUG_HTML = false;
const CHECK_INTERVAL = 1000 * 60 * 5; // 5 minutes

class App {
  readonly db: IDatabase;

  constructor(db: IDatabase) {
    this.db = db;
  }

  async scrape(makelaar: Makelaar): Promise<IHuis[]> {
    let responseData: unknown;
    if (USE_DEBUG_HTML) {
      console.warn(`Using debug response file for ${makelaar.name}`);
      if (!makelaar.debugResponseFile) {
        console.warn(`Debug response file not set for ${makelaar.name}`);
        return [];
      }
      responseData = fs.readFileSync(
        `resources/debugResponseFiles/${makelaar.debugResponseFile}`,
        "utf8",
      );
    } else {
      const response = await axios.get(makelaar.scrapeUrl);
      responseData = response.data;
    }
    // write respons.data to file for debugging
    // fs.writeFileSync('test-vandaal-makelaardij.json', responseData)
    // throw new Error("HELLO");

    const processor = domProcessors[makelaar.platformType];
    if (processor) {
      return processor.processDom(responseData, makelaar.url);
    }

    throw new Error(`No processor found for makelaar ${makelaar.name}`);
  }

  async main() {
    this.db.connect();

    const currentHouses = await this.db.getHouses();
    console.log("Current houses known:", currentHouses.size);
    let totalNewHouses = 0;
    for (const makelaar of makelaars) {
      const huizen = await this.scrape(makelaar);
      console.log(`Scraped ${huizen.length} huizen from ${makelaar.name}`);

      // Filter out houses that are already in the database, then drop any
      // non-independent dwellings (rooms / onzelfstandige woonruimte).
      const newHouses = huizen
        .filter((huis) => !currentHouses.has(huis.url))
        .filter((huis) => !isNonIndependentDwelling(huis));
      console.log(`Found ${newHouses.length} new houses`);

      // Persist new houses; the responder service watches the DB and
      // handles all listing-facing Telegram messaging.
      await this.db.saveHouses(newHouses);
      totalNewHouses += newHouses.length;
    }
    console.log(`Cycle done — ${totalNewHouses} new houses found`);
    this.db.disconnect();
  }
}

/**
 * Initialize the database
 */
// const db = new JsonDatabase();
const db = new SQLiteDatabase();

/**
 * Initialize the app
 */
const app = new App(db);
const doCheck = () => {
  app
    .main()
    .then(() => {
      console.log("Done");
    })
    .catch((error) => {
      console.error("Error", error);
    });
};

setInterval(() => {
  doCheck();
}, CHECK_INTERVAL);
doCheck();

console.log("App started, interval:", CHECK_INTERVAL);
