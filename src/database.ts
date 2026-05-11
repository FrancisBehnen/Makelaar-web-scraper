import { Database } from "bun:sqlite";
import { Huis, IHuis, isHuisDTO } from "./Huis";

export interface IDatabase {
  connect(): void;

  disconnect(): void;

  saveHouses(houses: IHuis[]): Promise<void>;

  getHouses(): Promise<Map<string, IHuis>>;
}

export class SQLiteDatabase implements IDatabase {
  private db?: Database;

  constructor(private dbPath: string = "db.sqlite") {}

  public connect() {
    this.db = new Database(this.dbPath);
    this.db.run(`CREATE TABLE IF NOT EXISTS houses (
      url TEXT PRIMARY KEY,
      straatnaamHuisnummer TEXT,
      plaats TEXT,
      vraagprijs TEXT,
      oppervlakte TEXT,
      kamers TEXT
    )`);
    console.log("Connected to SQLite database");
  }

  public disconnect() {
    if (!this.db) {
      console.error("Database is not connected");
      return;
    }
    this.db.close();
    console.log("Disconnected from SQLite database");
  }

  public async saveHouses(houses: IHuis[]): Promise<void> {
    if (!this.db) {
      throw new Error("Database is not connected");
    }
    const stmt = this.db.prepare(
      `INSERT OR REPLACE INTO houses (straatnaamHuisnummer, plaats, vraagprijs, oppervlakte, kamers, url) VALUES (?, ?, ?, ?, ?, ?)`,
    );
    for (const house of houses) {
      stmt.run(
        house.straatnaamHuisnummer,
        house.plaats,
        house.vraagprijs,
        house.oppervlakte,
        house.kamers,
        house.url,
      );
    }
  }

  public async getHouses(): Promise<Map<string, IHuis>> {
    if (!this.db) {
      throw new Error("Database is not connected");
    }
    const rows = this.db.query("SELECT * FROM houses").all();
    const houses = new Map<string, IHuis>();
    for (const row of rows) {
      if (!isHuisDTO(row)) {
        console.error("Invalid row in database", row);
        continue;
      }
      houses.set(
        row.url,
        new Huis(
          row.straatnaamHuisnummer,
          row.plaats,
          row.vraagprijs,
          row.oppervlakte,
          row.kamers,
          row.url,
        ),
      );
    }
    return houses;
  }
}
