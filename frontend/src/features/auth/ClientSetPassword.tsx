import React, { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ClientInvitationError, getClientInvitationAPI, setClientPasswordAPI } from "../../api/auth";
import {
  FieldError,
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  useFormValidation,
} from "../../validation";

/**
 * Where an invited client chooses their password.
 *
 * The invitation email's button opens `/client/set-password/:token`. This is a
 * public page -- the person has no account to sign in to yet -- and the token in
 * the address is the whole credential, so the page does three things with it and
 * nothing else:
 *
 *  1. On load it asks which account the link is for. That is read-only, so
 *     opening the page (or a mail scanner fetching it) uses nothing up, and a
 *     link that is dead is reported *before* anyone types a password.
 *  2. On submit it sends the chosen password, which accepts the invitation and
 *     activates the account. No session is issued.
 *  3. It then sends the client to the sign-in screen, to enter the address and
 *     password they just chose -- which proves the password works, and is the
 *     step the product asked for.
 *
 * The password is checked by the shared `password` rule (length only) and sent
 * exactly as typed -- never trimmed or altered; see docs/VALIDATION.md,
 * "Passwords are special". The server applies the same rule and is the authority.
 */

const EyeIcon = () => (
  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
  </svg>
);

const EyeOffIcon = () => (
  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
  </svg>
);

type Phase = "checking" | "ready" | "invalid" | "unavailable";

const fieldClass =
  "w-full rounded-xl border-2 border-[#E2E8F0] py-3 pl-4 pr-12 text-sm text-[#0F172A] shadow-sm outline-none placeholder-[#94A3B8] transition focus:border-[#2563EB] focus:ring-2 focus:ring-[#2563EB]/15 disabled:bg-[#F8FAFC] disabled:text-[#64748B] read-only:bg-[#F8FAFC] read-only:text-[#64748B]";

const labelClass = "block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-1";

export const ClientSetPassword: React.FC = () => {
  const { token = "" } = useParams<{ token: string }>();
  const navigate = useNavigate();

  const [phase, setPhase] = useState<Phase>("checking");
  // Bumped by "Try again" so the check re-runs without a page reload.
  const [attempt, setAttempt] = useState(0);
  const [email, setEmail] = useState("");

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const form = useFormValidation({
    password: { rule: "password", label: "Password" },
  });

  useEffect(() => {
    let cancelled = false;
    setPhase("checking");
    getClientInvitationAPI(token)
      .then((invitation) => {
        if (cancelled) return;
        setEmail(invitation.email);
        setPhase("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setPhase(err instanceof ClientInvitationError && err.kind === "invalid" ? "invalid" : "unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [token, attempt]);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setError(null);

    const check = form.validateAll({ password });
    // Confirmation is a relationship between two fields rather than a property
    // of one, so it is checked here and reported against the field to retype.
    let mismatch: string | null = null;
    if (!confirm) mismatch = "Please confirm your password.";
    else if (confirm !== password) mismatch = "The two passwords do not match.";
    setConfirmError(mismatch);
    if (!check.ok || mismatch) return;

    setSubmitting(true);
    try {
      const accepted = await setClientPasswordAPI(token, password);
      // Back to sign-in, with the address filled in and a note saying why.
      navigate("/login?client_invite=password_set", { replace: true, state: { email: accepted.email } });
    } catch (err) {
      if (err instanceof ClientInvitationError && err.kind === "invalid") {
        setPhase("invalid");
      } else {
        setError(err instanceof Error ? err.message : "Could not set your password. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex font-sans bg-white">
      <div className="flex-1 flex flex-col justify-center py-12 px-4 sm:px-6 lg:flex-none lg:w-[480px] xl:w-[560px] lg:px-20 xl:px-24 border-r border-[#E2E8F0]">
        <div className="mx-auto w-full max-w-sm">
          {phase === "checking" && (
            <p role="status" className="text-center text-sm text-[#64748B]">
              Checking your invitation…
            </p>
          )}

          {phase === "invalid" && (
            <div role="alert">
              <h2 className="text-2xl font-extrabold text-[#0F172A] tracking-tight">This link can't be used</h2>
              <p className="mt-3 text-sm leading-6 text-[#64748B]">
                This invitation link is invalid or has expired. It may already have been used, or a newer
                invitation may have replaced it. Ask the person who invited you to send a new one.
              </p>
              <Link
                to="/login"
                className="mt-6 inline-flex rounded-md bg-[#2563EB] px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700"
              >
                Go to sign in
              </Link>
            </div>
          )}

          {phase === "unavailable" && (
            <div role="alert">
              <h2 className="text-2xl font-extrabold text-[#0F172A] tracking-tight">Couldn't open your invitation</h2>
              <p className="mt-3 text-sm leading-6 text-[#64748B]">
                We couldn't reach Monitra just now. Check your connection and try again. Your invitation link has
                not been used up.
              </p>
              <button
                type="button"
                onClick={() => setAttempt((current) => current + 1)}
                className="mt-6 inline-flex rounded-md bg-[#2563EB] px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700"
              >
                Try again
              </button>
            </div>
          )}

          {phase === "ready" && (
            <>
              <div className="mb-8 text-center lg:text-left">
                <h2 className="text-3xl font-extrabold text-[#0F172A] tracking-tight">Set your password</h2>
                <p className="mt-2 text-sm text-[#64748B]">
                  Choose a password for your Monitra client account. You'll use it, with your email address, to
                  sign in.
                </p>
              </div>

              <form className="space-y-6" onSubmit={handleSubmit} noValidate>
                {error && (
                  <div role="alert" className="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-600">
                    {error}
                  </div>
                )}

                <div>
                  <label htmlFor="client-email" className={labelClass}>
                    Email Address
                  </label>
                  {/* Read-only, and the field password managers pair the new
                      password with: without a username beside it they save a
                      password for nobody. */}
                  <input
                    id="client-email"
                    name="username"
                    type="email"
                    autoComplete="username"
                    value={email}
                    readOnly
                    className={fieldClass}
                  />
                </div>

                <div>
                  <label htmlFor="client-password" className={labelClass}>
                    Password
                  </label>
                  <div className="relative">
                    <input
                      id="client-password"
                      name="new-password"
                      type={showPassword ? "text" : "password"}
                      autoComplete="new-password"
                      autoFocus
                      disabled={submitting}
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      onBlur={() => form.validateField("password", password)}
                      placeholder="••••••••"
                      {...form.fieldProps("password")}
                      className={fieldClass}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((current) => !current)}
                      aria-label={showPassword ? "Hide password" : "Show password"}
                      className="absolute inset-y-0 right-0 flex items-center pr-4 text-[#94A3B8] hover:text-[#64748B] focus:outline-none"
                    >
                      {showPassword ? <EyeOffIcon /> : <EyeIcon />}
                    </button>
                  </div>
                  <FieldError id={form.errorId("password")} message={form.errors.password} />
                  <p className="mt-1 text-xs text-[#94A3B8]">
                    At least {PASSWORD_MIN_LENGTH} characters. Any character is allowed.
                  </p>
                </div>

                <div>
                  <label htmlFor="client-confirm-password" className={labelClass}>
                    Confirm Password
                  </label>
                  <input
                    id="client-confirm-password"
                    name="confirm-password"
                    type={showPassword ? "text" : "password"}
                    autoComplete="new-password"
                    disabled={submitting}
                    value={confirm}
                    onChange={(event) => setConfirm(event.target.value)}
                    placeholder="••••••••"
                    maxLength={PASSWORD_MAX_LENGTH}
                    aria-invalid={confirmError ? true : undefined}
                    aria-describedby={confirmError ? "client-confirm-password-error" : undefined}
                    className={fieldClass}
                  />
                  <FieldError id="client-confirm-password-error" message={confirmError} />
                </div>

                <button
                  type="submit"
                  disabled={submitting}
                  className="w-full flex justify-center py-2.5 px-4 border border-transparent rounded-md shadow-sm text-sm font-semibold text-white bg-[#2563EB] hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-[#2563EB] disabled:opacity-50 disabled:cursor-not-allowed transition duration-150"
                >
                  {submitting ? "Saving…" : "Set password"}
                </button>
              </form>
            </>
          )}
        </div>
      </div>

      <div className="hidden lg:flex flex-1 relative bg-[#F8FAFC] items-center justify-center p-12">
        <div className="absolute inset-0 bg-gradient-to-br from-blue-50/50 to-purple-50/50" />
        <div className="relative flex flex-col items-center max-w-2xl text-center">
          <div className="rounded-2xl p-8 mb-8 bg-white/40 backdrop-blur-3xl shadow-xl ring-1 ring-black/5">
            <img src="/logo.png" alt="Monitra" className="w-[400px] object-contain drop-shadow-2xl" />
          </div>
          <h3 className="text-2xl font-bold tracking-tight text-[#0F172A] mb-3">Welcome to Monitra</h3>
          <p className="text-[#64748B] text-lg">
            Follow the progress of the projects shared with you, in one place.
          </p>
        </div>
      </div>
    </div>
  );
};
