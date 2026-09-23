import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "./authContext";
import {
  FieldError,
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  useFormValidation,
} from "../../validation";

/** Read once on mount: whether this load is a client invitation's Reject
 * redirect (`/login?client_invite=rejected`), so the reason can be shown
 * without it reappearing after the query string is cleaned up. */
const consumeRejectedInviteFlag = (): boolean => {
  try {
    const params = new URLSearchParams(window.location.search);
    if (params.get("client_invite") !== "rejected") return false;
    params.delete("client_invite");
    const query = params.toString();
    window.history.replaceState(
      {},
      document.title,
      `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`
    );
    return true;
  } catch {
    return false;
  }
};

const EyeIcon = () => (
  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
  </svg>
);

const EyeOffIcon = () => (
  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
  </svg>
);

const UserIcon = () => (
  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.75 7.5a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.5 20.25a7.5 7.5 0 0115 0" />
  </svg>
);

const LockIcon = () => (
  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16.5 10.5V7.75a4.5 4.5 0 00-9 0v2.75m-1.5 0h12a1.5 1.5 0 011.5 1.5v7.5a1.5 1.5 0 01-1.5 1.5h-12A1.5 1.5 0 015 19.5V12a1.5 1.5 0 011.5-1.5z" />
  </svg>
);

export const LoginScreen: React.FC = () => {
  const { login, loginAsClient, ssoError } = useAuth();
  const navigate = useNavigate();

  // Login states
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  // A failed `?token=...` handoff explains itself here rather than dropping the
  // user on an empty form with no idea why they were not signed in.
  const [error, setError] = useState<string | null>(ssoError);
  const [isLoading, setIsLoading] = useState(false);
  const [rejectedInvite] = useState<boolean>(consumeRejectedInviteFlag);

  // Forgot password modal states
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showCurrentPassword, setShowCurrentPassword] = useState(false);
  const [showNewPassword, setShowNewPassword] = useState(false);
  const [modalLoading, setModalLoading] = useState(false);
  const [modalSuccess, setModalSuccess] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  // Sign-in checks the credential rule, not the password rule: an account made
  // before today's policy may hold a shorter secret, and refusing to send it
  // would lock that person out of their own account.
  const loginForm = useFormValidation({
    email: { rule: "email", label: "Email address", required: true },
    password: { rule: "password", label: "Password", credential: true },
  });

  // The new password is being *chosen*, so the full length policy applies here.
  const passwordForm = useFormValidation({
    currentPassword: { rule: "password", label: "Current password", credential: true },
    newPassword: { rule: "password", label: "New password" },
  });

  // A client, signing in with just their email: either the Password field is
  // blank, or it holds the same address as Email. The second case is
  // deliberate, not a coincidence to guard against -- a browser that
  // autofills a saved credential routinely fills an identical value into
  // both fields (exactly what happened when this was tested), and typing the
  // email into Password is also the simplest thing for a client with no
  // password to reach for. Either way, only the email needs to be a
  // well-formed address -- the password rule never applies to this path, and
  // there is no link to click: `loginAsClient` signs them in immediately.
  const isClientAttempt =
    !password.trim() || password.trim().toLowerCase() === email.trim().toLowerCase();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (isClientAttempt) {
      const emailCheck = loginForm.validateField("email", email);
      if (!emailCheck.ok) return;

      setIsLoading(true);
      try {
        await loginAsClient(emailCheck.value as string);
        navigate("/client/dashboard");
      } catch (err: any) {
        setError(err.message || "Sorry, you are not registered as a client. Please contact your admin.");
      } finally {
        setIsLoading(false);
      }
      return;
    }

    const check = loginForm.validateAll({ email, password });
    if (!check.ok) {
      return;
    }

    setIsLoading(true);

    try {
      // The normalised address is what goes to the API — trimmed, with only the
      // domain lower-cased. The password is sent exactly as typed.
      await login(check.values.email as string, password);
      navigate("/dashboard");
    } catch (err: any) {
      setError(err.message || "Failed to sign in. Please verify your credentials or server status.");
    } finally {
      setIsLoading(false);
    }
  };

  const closePasswordModal = () => {
    setIsModalOpen(false);
    setModalSuccess(false);
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
    passwordForm.clear();
  };

  const handleUpdatePassword = async (e: React.FormEvent) => {
    e.preventDefault();

    const check = passwordForm.validateAll({ currentPassword, newPassword });
    // Confirmation is a relationship between two fields rather than a property
    // of one, so it is checked here and reported against the field the user
    // would retype.
    let mismatch: string | null = null;
    if (!confirmPassword) mismatch = "Please confirm your new password.";
    else if (confirmPassword !== newPassword) mismatch = "The two passwords do not match.";
    setConfirmError(mismatch);
    if (!check.ok || mismatch) return;

    setModalLoading(true);
    setModalSuccess(false);

    // Simulate API call for updating password
    setTimeout(() => {
      setModalLoading(false);
      setModalSuccess(true);
      setTimeout(() => {
        closePasswordModal();
      }, 2000);
    }, 1000);
  };

  return (
    <div className="min-h-screen flex font-sans bg-white">
      {/* Left Form Side */}
      <div className="flex-1 flex flex-col justify-center py-12 px-4 sm:px-6 lg:flex-none lg:w-[480px] xl:w-[560px] lg:px-20 xl:px-24 border-r border-[#E2E8F0]">
        <div className="mx-auto w-full max-w-sm">
          <div className="mb-10 text-center lg:text-left">
            {/* <img src="/logo.png" alt="Monitra Logo" className="h-14 w-auto mx-auto lg:mx-0 object-contain drop-shadow-sm mb-6" /> */}
            <h2 className="text-3xl font-extrabold text-[#0F172A] tracking-tight">
              Welcome back
            </h2>
            <p className="mt-2 text-sm text-[#64748B]">
              Sign in to your Monitra workspace
            </p>
          </div>

          <div className="mt-8">
          {rejectedInvite && (
            <div className="mb-6 p-3 bg-slate-50 border border-[#E2E8F0] rounded-md text-sm text-[#475569]">
              You have declined this client invitation. If this was a mistake, ask
              the person who invited you to send a new one.
            </div>
          )}

          <form className="space-y-6" onSubmit={handleSubmit}>
            {error && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-md text-sm text-red-600">
                {error}
              </div>
            )}

            <div>
              <label htmlFor="email" className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-1">
                Email Address
              </label>
              <div className="relative">
                <span className="pointer-events-none absolute left-3 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md bg-[#EFF6FF] text-[#2563EB]">
                  <UserIcon />
                </span>
                <input
                  id="email"
                  name="email"
                  type="email"
                  autoComplete="username"
                  required
                  disabled={isLoading}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  onBlur={() => loginForm.validateField("email", email)}
                  placeholder="you@example.com"
                  {...loginForm.fieldProps("email")}
                  className="w-full rounded-xl border-2 border-[#E2E8F0] py-3 pl-14 pr-3 text-sm text-[#0F172A] shadow-sm outline-none placeholder-[#94A3B8] transition focus:border-[#2563EB] focus:ring-2 focus:ring-[#2563EB]/15"
                />
              </div>
              <FieldError id={loginForm.errorId("email")} message={loginForm.errors.email} />
            </div>

            <div>
              <div className="flex justify-between items-center mb-1">
                <label htmlFor="password" className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase">
                  Password
                </label>
                <button
                  type="button"
                  onClick={() => setIsModalOpen(true)}
                  className="text-xs font-medium text-[#2563EB] hover:text-blue-700 focus:outline-none"
                >
                  Forgot password?
                </button>
              </div>
              <div className="relative">
                <span className="pointer-events-none absolute left-3 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md bg-[#EFF6FF] text-[#2563EB]">
                  <LockIcon />
                </span>
                <input
                  id="password"
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  disabled={isLoading}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  {...loginForm.fieldProps("password")}
                  className="w-full rounded-xl border-2 border-[#E2E8F0] py-3 pl-14 pr-16 text-sm text-[#0F172A] shadow-sm outline-none placeholder-[#94A3B8] transition focus:border-[#2563EB] focus:ring-2 focus:ring-[#2563EB]/15"
                />
                <div className="absolute inset-y-0 right-0 flex items-center gap-1 pr-3">
                  {/* Browsers routinely autofill a saved credential into this
                      field even when the person never typed anything — which
                      silently defeats "leave this blank to sign in as a
                      client". A one-click clear is the honest fix: it does not
                      guess that autofill happened, it just makes emptying the
                      field require no retyping. */}
                  {password && (
                    <button
                      type="button"
                      onClick={() => setPassword("")}
                      title="Clear password (sign in as a client instead)"
                      className="flex items-center text-[#94A3B8] hover:text-[#64748B] focus:outline-none"
                    >
                      <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="flex items-center text-[#94A3B8] hover:text-[#64748B] focus:outline-none"
                  >
                    {showPassword ? <EyeOffIcon /> : <EyeIcon />}
                  </button>
                </div>
              </div>
              <FieldError id={loginForm.errorId("password")} message={loginForm.errors.password} />
              <p className="mt-2 text-xs text-[#94A3B8]">
                Signing in as a client? Leave this blank, or enter your email
                address again here — your email is your whole credential, and
                you'll be signed in right away.
              </p>
            </div>

            <div>
              <button
                type="submit"
                disabled={isLoading}
                className="w-full flex justify-center py-2.5 px-4 border border-transparent rounded-md shadow-sm text-sm font-semibold text-white bg-[#2563EB] hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-[#2563EB] disabled:opacity-50 disabled:cursor-not-allowed transition duration-150"
              >
                {isLoading ? (
                  <div className="flex items-center gap-2">
                    <svg className="animate-spin h-5 w-5 text-white" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                    </svg>
                    Signing in...
                  </div>
                ) : (
                  "Sign in"
                )}
              </button>
            </div>
          </form>

          {/* The only public route out of here. Without this the download page
              is reachable only by someone who already knows the URL, which is
              nobody who has just been told to install Monitra. */}
          <p className="mt-8 text-center text-sm text-[#64748B]">
            Need the desktop app?{' '}
            <Link to="/download" className="font-semibold text-[#2563EB] hover:text-blue-700">
              Download Monitra
            </Link>
          </p>
          </div>
        </div>
      </div>

      {/* Right Slider/Hero Side */}
      <div className="hidden lg:flex flex-1 relative bg-[#F8FAFC] items-center justify-center p-12">
        <div className="absolute inset-0 bg-gradient-to-br from-blue-50/50 to-purple-50/50" />
        <div className="relative flex flex-col items-center max-w-2xl text-center">
          <div className="rounded-2xl p-8 mb-8 bg-white/40 backdrop-blur-3xl shadow-xl ring-1 ring-black/5">
             <img src="/logo.png" alt="Monitra" className="w-[400px] object-contain drop-shadow-2xl" />
          </div>
          <h3 className="text-2xl font-bold tracking-tight text-[#0F172A] mb-3">
            Enterprise Staff Management
          </h3>
          <p className="text-[#64748B] text-lg">
            Track time, manage projects, and analyze team performance all in one place.
          </p>
        </div>
      </div>

      {/* Forgot Password Modal */}
      {isModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-[#0f172a]/50 backdrop-blur-sm">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-sm overflow-hidden border border-[#E2E8F0]">
            <div className="px-6 py-4 border-b border-[#F1F5F9] flex justify-between items-center">
              <h3 className="text-[15px] font-bold text-[#0F172A]">Update Password</h3>
              <button
                type="button"
                onClick={closePasswordModal}
                className="text-[#94A3B8] hover:text-[#64748B] focus:outline-none"
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <form onSubmit={handleUpdatePassword} className="p-6 space-y-5">
              {modalSuccess && (
                <div className="p-3 bg-emerald-50 border border-emerald-200 rounded-md text-sm text-emerald-700">
                  Password updated successfully!
                </div>
              )}

              <div>
                <label className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-1">
                  Current Password
                </label>
                <div className="relative">
                  <input
                    type={showCurrentPassword ? "text" : "password"}
                    required
                    value={currentPassword}
                    onChange={(e) => setCurrentPassword(e.target.value)}
                    placeholder="••••••••"
                    {...passwordForm.fieldProps("currentPassword")}
                    className="w-full pl-3 pr-10 py-2 border border-[#E2E8F0] rounded-md shadow-sm focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-[#2563EB] text-sm text-[#0F172A]"
                  />
                  <button
                    type="button"
                    onClick={() => setShowCurrentPassword(!showCurrentPassword)}
                    className="absolute inset-y-0 right-0 pr-3 flex items-center text-[#94A3B8] hover:text-[#64748B] focus:outline-none"
                  >
                    {showCurrentPassword ? <EyeOffIcon /> : <EyeIcon />}
                  </button>
                </div>
                <FieldError
                  id={passwordForm.errorId("currentPassword")}
                  message={passwordForm.errors.currentPassword}
                />
              </div>

              <div>
                <label className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-1">
                  New Password
                </label>
                <div className="relative">
                  <input
                    type={showNewPassword ? "text" : "password"}
                    required
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    onBlur={() => passwordForm.validateField("newPassword", newPassword)}
                    placeholder="••••••••"
                    {...passwordForm.fieldProps("newPassword")}
                    className="w-full pl-3 pr-10 py-2 border border-[#E2E8F0] rounded-md shadow-sm focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-[#2563EB] text-sm text-[#0F172A]"
                  />
                  <button
                    type="button"
                    onClick={() => setShowNewPassword(!showNewPassword)}
                    className="absolute inset-y-0 right-0 pr-3 flex items-center text-[#94A3B8] hover:text-[#64748B] focus:outline-none"
                  >
                    {showNewPassword ? <EyeOffIcon /> : <EyeIcon />}
                  </button>
                </div>
                <FieldError
                  id={passwordForm.errorId("newPassword")}
                  message={passwordForm.errors.newPassword}
                />
                <p className="mt-1 text-xs text-[#94A3B8]">
                  At least {PASSWORD_MIN_LENGTH} characters. Any character is allowed.
                </p>
              </div>

              <div>
                <label
                  htmlFor="confirm-password"
                  className="block text-xs font-semibold text-[#94A3B8] tracking-wider uppercase mb-1"
                >
                  Confirm New Password
                </label>
                <input
                  id="confirm-password"
                  type={showNewPassword ? "text" : "password"}
                  required
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="••••••••"
                  maxLength={PASSWORD_MAX_LENGTH}
                  aria-invalid={confirmError ? true : undefined}
                  aria-describedby={confirmError ? "confirm-password-error" : undefined}
                  className="w-full pl-3 pr-10 py-2 border border-[#E2E8F0] rounded-md shadow-sm focus:outline-none focus:ring-2 focus:ring-[#2563EB] focus:border-[#2563EB] text-sm text-[#0F172A]"
                />
                <FieldError id="confirm-password-error" message={confirmError} />
              </div>

              <div className="pt-2">
                <button
                  type="submit"
                  disabled={modalLoading}
                  className="w-full flex justify-center py-2.5 px-4 border border-transparent rounded-md shadow-sm text-sm font-semibold text-white bg-[#2563EB] hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-[#2563EB] disabled:opacity-50 transition duration-150"
                >
                  {modalLoading ? "Updating..." : "Update Password"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
