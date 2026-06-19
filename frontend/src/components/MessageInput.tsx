import type { KeyboardEvent } from "react";

interface MessageInputProps {
  disabled: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
  value: string;
}

export function MessageInput({
  disabled,
  onChange,
  onSubmit,
  value,
}: MessageInputProps) {
  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      onSubmit();
    }
  }

  return (
    <div className="composer">
      <textarea
        className="composer__input"
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="输入问题后回车发送，例如：我想查询订单状态"
        rows={4}
        value={value}
      />
      <div className="composer__footer">
        <span className="composer__hint">Enter 发送，Shift + Enter 换行</span>
        <button
          className="primary-button"
          disabled={disabled || value.trim().length === 0}
          onClick={onSubmit}
          type="button"
        >
          {disabled ? "发送中..." : "发送"}
        </button>
      </div>
    </div>
  );
}
