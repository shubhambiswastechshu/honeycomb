"use client";

import type { ChangeEvent, ReactNode } from "react";
import type { OrganizationChoice } from "@/lib/api";
import Logo from "@/components/ui/Logo";

interface AuthCardProps {
  title: string;
  subtitle: string;
  children: ReactNode;
  /**
   * Change this when the heading text changes for a new reason (a wizard step,
   * say) and the header should re-animate rather than swap silently.
   */
  headingKey?: string | number;
}

/**
 * The sign-in / sign-up shell: the form column, and the honeycomb beside it on
 * a wide screen. The art is a sibling of the form rather than a background on
 * the page, so the form keeps its own centred measure whatever the window does.
 */
export function AuthCard({
  title,
  subtitle,
  children,
  headingKey,
}: AuthCardProps) {
  return (
    <div className="auth-split">
      <main className="card">
        <Logo />
        <header key={headingKey} className="card-heading">
          <h1 className="title">{title}</h1>
          <p className="subtitle">{subtitle}</p>
        </header>
        {children}
      </main>
      <div className="auth-art" role="presentation" />
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="error" role="alert">
      {message}
    </div>
  );
}

interface FieldProps {
  id: string;
  label: string;
  type: string;
  value: string;
  onChange: (value: string) => void;
  autoComplete: string;
  disabled: boolean;
  placeholder?: string;
  minLength?: number;
}

export function Field({
  id,
  label,
  type,
  value,
  onChange,
  autoComplete,
  disabled,
  placeholder,
  minLength,
}: FieldProps) {
  function handleChange(event: ChangeEvent<HTMLInputElement>) {
    onChange(event.target.value);
  }

  return (
    <div className="field">
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <input
        className="input"
        id={id}
        name={id}
        type={type}
        value={value}
        onChange={handleChange}
        autoComplete={autoComplete}
        placeholder={placeholder}
        minLength={minLength}
        disabled={disabled}
        required
      />
    </div>
  );
}

interface OrganizationFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: OrganizationChoice[];
  disabled: boolean;
}

/**
 * Shown only after sign in answers 409: the same address is valid in several
 * organizations and the user has to say which one they meant.
 */
export function OrganizationField({
  id,
  label,
  value,
  onChange,
  options,
  disabled,
}: OrganizationFieldProps) {
  function handleChange(event: ChangeEvent<HTMLSelectElement>) {
    onChange(event.target.value);
  }

  return (
    <div className="field">
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <select
        className="input"
        id={id}
        name={id}
        value={value}
        onChange={handleChange}
        disabled={disabled}
        required
      >
        {options.map(function renderOption(option) {
          return (
            <option key={option.slug} value={option.slug}>
              {option.name}
            </option>
          );
        })}
      </select>
    </div>
  );
}
