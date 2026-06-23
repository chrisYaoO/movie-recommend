import { isErrorStatus } from "../api";
import { RefreshIcon } from "./Icons";

export function StatusText({ value }: { value: string }) {
  return value ? <span className="status">{value}</span> : null;
}

export function StatusBanner({ value, onRetry }: { value: string; onRetry?: () => void }) {
  if (!value) return null;
  const error = isErrorStatus(value);
  return (
    <div className={error ? "status-banner error" : "status-banner"} role={error ? "alert" : "status"}>
      <span>{value}</span>
      {onRetry && (
        <button className="icon-button" onClick={onRetry} aria-label="Retry" title="Retry">
          <RefreshIcon />
        </button>
      )}
    </div>
  );
}
