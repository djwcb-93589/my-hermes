import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useBeforeUnload, useBlocker } from "react-router-dom";

import { HttpError } from "../../api/http";
import { useAuth } from "../../auth/AuthContext";
import {
  analyzeConfigDraft,
  createConfigDraft,
  markDraftSynchronized,
  type ConfigDraft,
  type ConfigDraftInput,
  updateDraftInput,
} from "./configDraft";
import type {
  ConfigConflictKind,
  ConfigPageViewModel,
  ConfigSaveResultViewModel,
} from "./configModels";
import { loadConfiguration, saveConfiguration } from "./configService";

type PagePhase = "loading" | "ready" | "error";
type SynchronizationState = "idle" | "refreshing" | "failed";
type LoadMode = "initial" | "manual" | "after_save";

export function useConfigController() {
  const { client } = useAuth();
  const readAbortRef = useRef<AbortController | null>(null);
  const saveAbortRef = useRef<AbortController | null>(null);
  const snapshotRef = useRef<ConfigPageViewModel | null>(null);
  const [phase, setPhase] = useState<PagePhase>("loading");
  const [snapshot, setSnapshot] = useState<ConfigPageViewModel | null>(null);
  const [draft, setDraft] = useState<ConfigDraft>(new Map());
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveResult, setSaveResult] =
    useState<ConfigSaveResultViewModel | null>(null);
  const [conflict, setConflict] = useState<ConfigConflictKind | null>(null);
  const [synchronization, setSynchronization] =
    useState<SynchronizationState>("idle");
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [controlDialogOpen, setControlDialogOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  const analysis = useMemo(
    () =>
      snapshot === null
        ? {
            changes: [],
            errors: new Map<string, string>(),
            hasUnsavedChanges: false,
          }
        : analyzeConfigDraft(snapshot, draft),
    [draft, snapshot],
  );

  const loadSnapshot = useCallback(
    async (mode: LoadMode): Promise<boolean> => {
      readAbortRef.current?.abort();
      const controller = new AbortController();
      readAbortRef.current = controller;
      setLoading(true);
      setLoadError(null);
      if (mode === "initial") {
        setPhase("loading");
      }
      if (mode === "after_save") {
        setSynchronization("refreshing");
      }
      try {
        const nextSnapshot = await loadConfiguration(
          client,
          controller.signal,
        );
        if (controller.signal.aborted) {
          return false;
        }
        snapshotRef.current = nextSnapshot;
        setSnapshot(nextSnapshot);
        setDraft(createConfigDraft(nextSnapshot));
        setPhase("ready");
        setConflict(null);
        setSaveError(null);
        setSynchronization("idle");
        return true;
      } catch (error: unknown) {
        if (controller.signal.aborted) {
          return false;
        }
        const message = configReadErrorMessage(error);
        if (mode === "after_save") {
          setSynchronization("failed");
        } else if (snapshotRef.current === null) {
          setPhase("error");
          setLoadError(message);
        } else {
          setLoadError(message);
        }
        return false;
      } finally {
        if (readAbortRef.current === controller) {
          readAbortRef.current = null;
          setLoading(false);
        }
      }
    },
    [client],
  );

  useEffect(() => {
    void loadSnapshot("initial");
    return () => {
      readAbortRef.current?.abort();
      readAbortRef.current = null;
      saveAbortRef.current?.abort();
      saveAbortRef.current = null;
    };
  }, [loadSnapshot]);

  useBeforeUnload(
    useCallback(
      (event) => {
        if (analysis.hasUnsavedChanges) {
          event.preventDefault();
          event.returnValue = "";
        }
      },
      [analysis.hasUnsavedChanges],
    ),
  );

  const blocker = useBlocker(analysis.hasUnsavedChanges);
  useEffect(() => {
    if (blocker.state !== "blocked") {
      return;
    }
    const leave = window.confirm(
      "当前配置页有未保存修改。离开页面将丢弃这些修改，是否继续？",
    );
    if (leave) {
      blocker.proceed();
    } else {
      blocker.reset();
    }
  }, [blocker]);

  const changeDraft = useCallback(
    (name: string, input: ConfigDraftInput): void => {
      setDraft((current) =>
        updateDraftInput(current, name, () => input),
      );
      setSaveError(null);
    },
    [],
  );

  const discardDraft = useCallback((): void => {
    if (snapshot === null || !analysis.hasUnsavedChanges) {
      return;
    }
    if (
      !window.confirm(
        "放弃后将恢复最近一次成功读取的配置快照，是否继续？",
      )
    ) {
      return;
    }
    setDraft(createConfigDraft(snapshot));
    setSaveError(null);
    setConflict(null);
  }, [analysis.hasUnsavedChanges, snapshot]);

  const refreshManually = useCallback((): void => {
    if (
      analysis.hasUnsavedChanges &&
      !window.confirm(
        "重新加载会丢弃当前本地草稿并读取最新配置，是否继续？",
      )
    ) {
      return;
    }
    void loadSnapshot(
      synchronization === "failed" ? "after_save" : "manual",
    );
  }, [analysis.hasUnsavedChanges, loadSnapshot, synchronization]);

  const loadLatestAfterConflict = useCallback((): void => {
    if (
      !window.confirm(
        "加载最新配置将丢弃当前本地草稿；旧草稿不会自动合并，是否继续？",
      )
    ) {
      return;
    }
    void loadSnapshot("manual");
  }, [loadSnapshot]);

  const openConfirmation = useCallback((): void => {
    if (
      analysis.changes.length === 0 ||
      analysis.errors.size > 0 ||
      saving ||
      synchronization !== "idle"
    ) {
      return;
    }
    setConfirmationOpen(true);
  }, [analysis.changes.length, analysis.errors.size, saving, synchronization]);

  const continueToControlToken = useCallback((): void => {
    setConfirmationOpen(false);
    setControlDialogOpen(true);
  }, []);

  const closeControlDialog = useCallback((): void => {
    if (saveAbortRef.current !== null) {
      return;
    }
    setControlDialogOpen(false);
  }, []);

  const submitControlToken = useCallback(
    async (controlToken: string): Promise<"success" | "invalid" | "failed"> => {
      if (
        snapshot === null ||
        analysis.changes.length === 0 ||
        analysis.errors.size > 0 ||
        saving ||
        saveAbortRef.current !== null ||
        synchronization !== "idle"
      ) {
        return "failed";
      }
      const controller = new AbortController();
      saveAbortRef.current = controller;
      setSaving(true);
      setSaveError(null);
      setConflict(null);
      try {
        const result = await saveConfiguration(
          controlToken,
          snapshot.revision,
          analysis.changes,
          controller.signal,
        );
        if (controller.signal.aborted) {
          return "failed";
        }
        setSaveResult(result);
        setDraft((current) => markDraftSynchronized(current));
        setSynchronization("refreshing");
        void loadSnapshot("after_save");
        return "success";
      } catch (error: unknown) {
        if (controller.signal.aborted) {
          return "failed";
        }
        if (
          error instanceof HttpError &&
          (error.status === 401 || error.status === 403)
        ) {
          return "invalid";
        }
        if (error instanceof HttpError && error.status === 409) {
          setConflict(configConflictKind(error.publicCode));
          return "failed";
        }
        setSaveError(configSaveErrorMessage(error));
        return "failed";
      } finally {
        if (saveAbortRef.current === controller) {
          saveAbortRef.current = null;
        }
        setSaving(false);
      }
    },
    [analysis.changes, analysis.errors.size, loadSnapshot, saving, snapshot, synchronization],
  );

  return {
    phase,
    snapshot,
    draft,
    analysis,
    loading,
    loadError,
    saveError,
    saveResult,
    conflict,
    synchronization,
    confirmationOpen,
    controlDialogOpen,
    saving,
    changeDraft,
    discardDraft,
    refreshManually,
    loadLatestAfterConflict,
    openConfirmation,
    closeConfirmation: () => setConfirmationOpen(false),
    continueToControlToken,
    closeControlDialog,
    submitControlToken,
    retryInitialLoad: () => void loadSnapshot("initial"),
  };
}

function configConflictKind(publicCode: string | null): ConfigConflictKind {
  if (publicCode === "config_conflict") {
    return "revision";
  }
  if (publicCode === "config_shadowed") {
    return "shadowed";
  }
  return "unknown";
}

function configReadErrorMessage(error: unknown): string {
  if (error instanceof HttpError) {
    if (error.code === "invalid_response") {
      return "配置接口返回了无效的安全响应。";
    }
    if (error.status === 404) {
      return "配置文件当前不存在。";
    }
    if (error.status === 503) {
      return "配置读取服务暂时不可用。";
    }
    if (error.code === "request_timeout") {
      return "配置读取超时，请稍后重试。";
    }
    if (error.code === "network_unavailable") {
      return "网络暂时不可用，无法读取配置。";
    }
  }
  return "无法读取配置安全快照。";
}

function configSaveErrorMessage(error: unknown): string {
  if (error instanceof HttpError) {
    switch (error.publicCode) {
      case "config_invalid":
        return "完整配置未通过后端安全校验，未保存本地草稿。";
      case "config_field_unknown":
        return "提交字段已不受后端支持，请重新加载配置。";
      case "config_field_read_only":
        return "后端拒绝修改只读字段，请重新加载配置。";
      case "config_value_invalid":
        return "配置值未通过后端完整校验，请检查草稿。";
      case "config_not_found":
        return "配置文件当前不存在，修改未保存。";
      case "config_unavailable":
      case "config_write_failed":
        return "配置服务暂时无法安全写入，修改未保存。";
    }
    if (error.code === "request_timeout") {
      return "保存请求超时；请先重新加载配置确认结果，再决定是否重试。";
    }
    if (error.code === "network_unavailable") {
      return "网络在保存期间不可用；请先重新加载配置确认结果。";
    }
    if (error.code === "invalid_response") {
      return "保存接口返回了无效的安全响应。";
    }
    if (error.status === 400) {
      return "配置修改未通过后端校验，本地草稿已保留。";
    }
    if (error.status === 404) {
      return "配置文件当前不存在，本地草稿已保留。";
    }
    if (error.status === 503) {
      return "配置服务暂时不可用，本地草稿已保留。";
    }
  }
  return "配置未能安全保存，本地草稿已保留。";
}
