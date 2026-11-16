import { MailComWebAliasAddon, MAILCOM_ALIAS_DOMAINS } from "maildotcom-sdk/web-aliases";

export { MAILCOM_ALIAS_DOMAINS };

const MAILCOM_ALIAS_DOMAIN_SET = new Set(MAILCOM_ALIAS_DOMAINS);

export class MailComConfiguredAliasAddon extends MailComWebAliasAddon {
  async createAliasInConfiguredDomain(address, domain) {
    const normalizedAddress = String(address || "").trim().toLowerCase();
    const normalizedDomain = String(domain || "").trim().toLowerCase();
    if (!normalizedAddress.endsWith(`@${normalizedDomain}`) || !MAILCOM_ALIAS_DOMAIN_SET.has(normalizedDomain)) {
      const error = new Error(`Alias domain is not supported by mail.com: ${normalizedDomain}`);
      error.name = "MailComValidationError";
      throw error;
    }
    try {
      await this.ensureSettingsToken();
    } catch (error) {
      if (error && typeof error === "object") error.aliasMutationAttempted = false;
      throw error;
    }
    try {
      await this.settingsRequest("/mailaccount/primary/emailAddresses?absoluteURI=false", {
        method: "POST",
        headers: {
          Accept: "application/vnd.ui.trinity.minimalmailaddress-v3+json",
          "Content-Type": "application/vnd.ui.trinity.minimalmailaddress-v3+json",
        },
        body: JSON.stringify({
          address: normalizedAddress,
          deletable: true,
          pgpEnabled: false,
          defaultSenderAddress: false,
          defaultReceiverAddress: false,
          state: "ACTIVE",
        }),
      });
    } catch (error) {
      if (error && typeof error === "object") error.aliasMutationAttempted = true;
      throw error;
    }
    return { address: normalizedAddress };
  }
}
