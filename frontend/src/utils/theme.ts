import type { ThemeMode } from "../types";

export function themeLabel(mode: ThemeMode) {
  return { light: "Light", dark: "Dark", system: "System" }[mode];
}

export function nextThemeMode(mode: ThemeMode): ThemeMode {
  if (mode === "system") return "light";
  if (mode === "light") return "dark";
  return "system";
}
