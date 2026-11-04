"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, clearAuthToken } from "./api";
import { passwordConfirmMessage, passwordLengthMessage, roleOptionsFor,
  sortedUsersForTable, userCanBeDeleted, userCanBeManaged } from "./settingsPageHelpers";

const CREDENTIALS_CHANGED_NOTICE_KEY = "km_vms_credentials_changed_notice";

export default function useSettingsUsersController({ currentUser, lang, t, showToast, normalizedError, router }) {
  const [users, setUsers] = useState([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [userBusy, setUserBusy] = useState(false);
  const [usersModalOpen, setUsersModalOpen] = useState(false);
  const [userModal, setUserModal] = useState(null);
  const [userDeleteTarget, setUserDeleteTarget] = useState(null);
  const userBusyRef = useRef(false);
  const usersGenerationRef = useRef(0);
  const canManageUsers = Boolean(currentUser?.permissions?.includes("manage_users"));
  const canReadUsers = Boolean(currentUser?.permissions?.includes("admin_access"));
  const sortedUsers = useMemo(() => sortedUsersForTable(users), [users]);

  async function loadUsers() {
    const generation = ++usersGenerationRef.current;
    setUsersLoading(true);
    try {
      const data = await apiFetch("/users");
      if (generation === usersGenerationRef.current) setUsers(data);
    } catch (err) {
      if (generation === usersGenerationRef.current) showToast(normalizedError(err, lang, "users"));
    } finally {
      if (generation === usersGenerationRef.current) setUsersLoading(false);
    }
  }

  useEffect(() => {
    setUserModal(null);
    setUserDeleteTarget(null);
    if (canReadUsers) loadUsers();
    else { setUsers([]); setUsersLoading(false); }
    return () => { usersGenerationRef.current += 1; };
  }, [currentUser?.id, canReadUsers]);

  async function openUsersModal() {
    if (!canManageUsers || usersLoading) return;
    setUsersModalOpen(true);
    await loadUsers();
  }

  function openCreateUser() {
    const options = roleOptionsFor(currentUser);
    setUserModal({
      mode: "create",
      id: null,
      username: "",
      display_name: "",
      password: "",
      password_confirm: "",
      current_password: "",
      fieldErrors: {},
      role: options[0] || "viewer",
      is_active: true,
      error: "",
    });
  }

  function openEditUser(user) {
    setUserModal({
      mode: "edit",
      id: user.id,
      username: user.username,
      display_name: user.display_name || "",
      password: "",
      password_confirm: "",
      current_password: "",
      fieldErrors: {},
      role: user.role,
      is_active: Boolean(user.is_active),
      is_owner: user.role === "owner",
      error: "",
    });
  }

  function patchUserModal(key, value) {
    setUserModal((current) => ({
      ...current,
      [key]: value,
      error: key === "error" ? value : "",
      fieldErrors: {
        ...(current?.fieldErrors || {}),
        [key]: "",
      },
    }));
  }

  function rejectUserPassword(field, message) {
    setUserModal((current) => ({
      ...current,
      error: "",
      fieldErrors: {
        ...(current?.fieldErrors || {}),
        [field]: message,
      },
    }));
  }

  async function submitUserModal(event) {
    event.preventDefault();
    if (!userModal || userBusyRef.current) return;
    if (!userModal.username.trim()) {
      patchUserModal("error", t.usernameRequired);
      return;
    }
    if (userModal.mode === "create" && userModal.password.length < 8) {
      rejectUserPassword("password", passwordLengthMessage(lang));
      return;
    }
    const availableRoles = userModal.mode === "edit" && userModal.id === currentUser?.id
      ? [currentUser.role]
      : userModal.mode === "edit" && userModal.is_owner
      ? ["owner"]
      : roleOptionsFor(currentUser);
    if (!availableRoles.includes(userModal.role)) {
      patchUserModal("error", t.roleRequired);
      return;
    }
    if (userModal.mode === "edit" && userModal.password && userModal.password.length < 8) {
      rejectUserPassword("password", passwordLengthMessage(lang));
      return;
    }
    if ((userModal.mode === "create" || userModal.password) && userModal.password !== userModal.password_confirm) {
      rejectUserPassword("password_confirm", passwordConfirmMessage(lang));
      return;
    }
    if (userModal.mode === "edit" && userModal.id === currentUser?.id && userModal.password && !userModal.current_password) {
      patchUserModal("error", t.currentPasswordRequired || t.passwordRequired);
      return;
    }

    userBusyRef.current = true;
    setUserBusy(true);
    try {
      const body = {
        username: userModal.username.trim(),
        display_name: userModal.display_name.trim(),
        role: userModal.role,
        is_active: Boolean(userModal.is_active),
      };
      if (userModal.password) body.password = userModal.password;
      if (userModal.current_password) body.current_password = userModal.current_password;

      const changesOwnCredentials = userModal.mode === "edit"
        && userModal.id === currentUser?.id
        && (body.username !== currentUser?.username || Boolean(userModal.password));

      if (userModal.mode === "create") {
        await apiFetch("/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        showToast({ variant: "success", title: t.toasts.userCreatedTitle, text: userModal.username.trim() });
      } else {
        await apiFetch(`/users/${userModal.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (changesOwnCredentials) {
          setUserModal(null);
          try {
            window.sessionStorage.setItem(CREDENTIALS_CHANGED_NOTICE_KEY, "credentials_changed");
          } catch (_) {}
          clearAuthToken();
          router.replace("/login");
          return;
        }
        showToast({ variant: "success", title: t.toasts.userUpdatedTitle, text: userModal.username.trim() });
      }
      setUserModal(null);
      await loadUsers();
    } catch (err) {
      const errorToast = normalizedError(err, lang, "users");
      showToast(errorToast);
    } finally {
      userBusyRef.current = false;
      setUserBusy(false);
    }
  }

  async function toggleUserActive(user) {
    if (userBusyRef.current || !userCanBeManaged(currentUser, user) || user.id === currentUser?.id) return;
    userBusyRef.current = true;
    setUserBusy(true);
    try {
      await apiFetch(`/users/${user.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !user.is_active }),
      });
      showToast({
        variant: "success",
        title: user.is_active ? t.toasts.userDisabledTitle : t.toasts.userEnabledTitle,
        text: user.username,
      });
      await loadUsers();
    } catch (err) {
      showToast(normalizedError(err, lang, "users"));
    } finally {
      userBusyRef.current = false;
      setUserBusy(false);
    }
  }

  function requestDeleteUser(user) {
    if (userBusy || !userCanBeDeleted(currentUser, user, users)) return;
    setUserDeleteTarget(user);
  }

  async function deleteUser(user) {
    if (userBusyRef.current || !userCanBeDeleted(currentUser, user, users)) return;
    userBusyRef.current = true;
    setUserBusy(true);
    try {
      await apiFetch(`/users/${user.id}`, { method: "DELETE" });
      showToast({
        variant: "success",
        title: t.toasts.userDeletedTitle || (lang === "en" ? "User deleted" : "Пользователь удалён"),
        text: user.username,
      });
      setUserDeleteTarget(null);
      await loadUsers();
    } catch (err) {
      showToast(normalizedError(err, lang, "users"));
    } finally {
      userBusyRef.current = false;
      setUserBusy(false);
    }
  }

  return { users, setUsers, usersLoading, setUsersLoading, userBusy, setUserBusy, usersModalOpen, setUsersModalOpen, userModal, setUserModal, userDeleteTarget, setUserDeleteTarget, loadUsers, canManageUsers, sortedUsers, openUsersModal, openCreateUser, openEditUser, patchUserModal, rejectUserPassword, submitUserModal, toggleUserActive, requestDeleteUser, deleteUser };
}
