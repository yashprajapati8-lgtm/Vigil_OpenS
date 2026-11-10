// The VIGIL mark. One white silhouette (public/vigil-mark.png) used as a mask,
// so the same shape can be the brand (gradient) or a status light (green, amber,
// red, grey) without a second asset.

export type LogoTone = "brand" | "ok" | "warn" | "bad" | "idle";

export function Logo({ size = 28, tone = "brand", pulse = false, label }: {
  size?: number; tone?: LogoTone; pulse?: boolean; label?: string;
}) {
  return (
    <span className={`logo-glow logo-glow-${tone}`} data-pulse={pulse || undefined}
      role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}
      title={label}>
      <span className={`logo logo-${tone}`} style={{ width: size, height: size }} />
    </span>
  );
}
