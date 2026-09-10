function randomId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : Math.random().toString(36).slice(2);
}

export function newId(prefix: string): string {
  return `${prefix}_${randomId()}`;
}
