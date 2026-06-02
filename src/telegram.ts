import TelegramBot from "node-telegram-bot-api";
import { IHuis } from "./Huis";

const token = process.env.TELEGRAM_BOT_TOKEN;
if (!token) {
  throw new Error("TELEGRAM_BOT_TOKEN is not set");
}

const bot = new TelegramBot(token, { polling: true });

bot.on("message", async (msg) => {
  const chatId = msg.chat.id;
  console.log(msg.from?.id, msg);

  if (msg.text === "/start") {
    await bot.sendMessage(chatId, "Welcome to the house bot!");
    return;
  }

  if (msg.text === "/stop") {
    await bot.sendMessage(chatId, "Goodbye!");
    return;
  }

  // No generic acknowledgement: incoming messages other than the known
  // commands above are intentionally left unanswered.
});

/**
 * Standard introduction letter sent to the makelaar. The {@link ADRES_PLACEHOLDER}
 * is replaced with the listing's street + house number before sending.
 */
const ADRES_PLACEHOLDER = "[[ADRES]]";

const AANMELDBRIEF_TEMPLATE = `Geachte meneer, mevrouw,

Zojuist zagen wij jullie woning aan de ${ADRES_PLACEHOLDER}. Mijn partner Francis en ik, Corlien, zijn op zoek naar een eerste thuis om in te gaan samenwonen, nu wij allebei onderhand zijn afgestudeerd en aan banen zijn gestart. We hebben elkaar leren kennen via de Delftse studentenzeilvereniging en zijn zo verliefd geworden op Delft, dat we hier in de regio zouden willen blijven wonen. We zien hier ons al helemaal wonen! Graag zouden wij ons daarom aan willen melden voor de bezichtiging van het appartement.

Zelf ben ik recent afgestudeerd scheikundige en start ik deze maand als junior chemicus bij Lignitec, een Delftse startup in biobouwmaterialen. Daarnaast ben ik al een aantal jaar werkzaam als retailspecialist bij Sounds, een platenzaak in het centrum van Delft. Daar ben ik tijdens mijn studententijd terecht gekomen als bijbaan, omdat ik al jaren LP's verzamel. Daar blijf ik nog part-time werkzaam. Mijn inkomen zit vanaf deze maand gecombineerd tussen de €2000 - €2500 per maand.

Mijn partner Francis werkt als AI-specialist bij Coolblue in Rotterdam, waar hij met veel enthousiasme werkt aan de toekomst van online retail. In 2024 heeft hij zijn master Technische Informatica (Computer Science) afgerond aan de TU Delft. Als hij thuiskomt van werk, vindt hij het leuk om lekker te koken. Hij verdient tussen de €3000 - €3500 per maand.

Wij zouden graag de woning komen bezichtigen. Zou u ons kunnen laten weten wanneer de bezichtiging is en of wij zouden mogen komen? We zien uit naar uw reactie! Bij voorbaat hartelijk dank voor uw tijd.

Met vriendelijke groet,
Corlien Douma
+31646853193`;

/** Escape the characters that are special inside Telegram HTML messages. */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * Build the aanmeldbrief for a given address, wrapped in a <pre> code block so
 * Telegram clients render it with a one-tap copy button. (Telegram's dedicated
 * copy_text inline button is capped at 256 characters, too short for the letter.)
 */
function formatAanmeldbrief(straatnaamHuisnummer: string): string {
  const brief = AANMELDBRIEF_TEMPLATE.replace(
    ADRES_PLACEHOLDER,
    straatnaamHuisnummer,
  );
  return `<pre>${escapeHtml(brief)}</pre>`;
}

export interface IMessenger {
  sendNewHouses(houses: IHuis[]): Promise<void>;
}

export class TelegramMessenger implements IMessenger {
  readonly chatIds: string[];

  constructor(chatIds: string[]) {
    this.chatIds = chatIds;
  }

  async sendNewHouses(houses: IHuis[]): Promise<void> {
    for (const house of houses) {
      const houseFormatted = house.formatForTelegram();
      const aanmeldbrief = formatAanmeldbrief(house.straatnaamHuisnummer);
      const message = `🚨 <b>Nieuw huis gevonden!</b> 🚨\n\n<blockquote>Gegevens van het huis:\n${houseFormatted}</blockquote>\n\n📋 <b>Aanmeldbrief</b> (tik op het kopieer-icoon):\n${aanmeldbrief}`;
      for (const chatId of this.chatIds) {
        await bot.sendMessage(chatId, message, {
          parse_mode: "HTML",
        });
      }
    }
  }
}
