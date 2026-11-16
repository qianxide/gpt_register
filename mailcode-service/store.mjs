import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export class MailboxStore {
  constructor(directory) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.path = join(directory, "accounts.enc");
    const keyPath = join(directory, "accounts.key");
    if (!existsSync(keyPath)) {
      if (existsSync(this.path)) throw new Error("The mailbox encryption key is missing");
      writeFileSync(keyPath, randomBytes(32), { mode: 0o600, flag: "wx" });
    }
    this.key = readFileSync(keyPath);
    if (this.key.length !== 32) throw new Error("Invalid mailbox encryption key");
    this.data = { version: 1, accounts: {}, aliases: {}, proxyPool: { enabled: false, proxies: [], generation: 0 } };
    if (existsSync(this.path)) {
      const envelope = JSON.parse(readFileSync(this.path, "utf8"));
      const decipher = createDecipheriv("aes-256-gcm", this.key, Buffer.from(envelope.iv, "base64"));
      decipher.setAuthTag(Buffer.from(envelope.tag, "base64"));
      const plaintext = Buffer.concat([decipher.update(Buffer.from(envelope.data, "base64")), decipher.final()]);
      this.data = JSON.parse(plaintext.toString("utf8"));
      if (this.data.version !== 1 || !this.data.accounts || !this.data.aliases) throw new Error("Invalid mailbox state");
      if (!this.data.proxyPool || typeof this.data.proxyPool !== "object") {
        this.data.proxyPool = { enabled: false, proxies: [], generation: 0 };
      }
    }
  }

  save() {
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.key, iv);
    const encrypted = Buffer.concat([cipher.update(JSON.stringify(this.data), "utf8"), cipher.final()]);
    const temporary = `${this.path}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify({
      iv: iv.toString("base64"), tag: cipher.getAuthTag().toString("base64"), data: encrypted.toString("base64"),
    }), { mode: 0o600 });
    renameSync(temporary, this.path);
  }
}
