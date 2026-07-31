import { useRef } from "react";

import { ConfigConflictPanel } from "./components/ConfigConflictPanel";
import { ConfigGroup } from "./components/ConfigGroup";
import { ConfigSaveBar } from "./components/ConfigSaveBar";
import { ConfigSaveConfirmation } from "./components/ConfigSaveConfirmation";
import { ConfigSaveResult } from "./components/ConfigSaveResult";
import { ControlTokenDialog } from "./components/ControlTokenDialog";
import { useConfigController } from "./useConfigController";

export function ConfigPage() {
  const controller = useConfigController();
  const saveButtonRef = useRef<HTMLButtonElement>(null);
  const busy =
    controller.loading ||
    controller.saving ||
    controller.synchronization === "refreshing";

  return (
    <>
      <header className="hero config-hero">
        <div>
          <p className="eyebrow">SAFE CONFIGURATION</p>
          <h1>配置可见，修改受控</h1>
          <p>
            仅展示后端明确公开的配置投影。普通可写字段通过 revision 和一次性 Control Token 保存；敏感值始终不可见。
          </p>
        </div>
        <div className="hero-actions">
          <span className="controlled-chip">CONTROLLED WRITE</span>
          <button
            type="button"
            className="secondary-button"
            disabled={busy}
            onClick={controller.refreshManually}
          >
            重新加载配置
          </button>
        </div>
      </header>

      <aside className="configuration-boundary-note">
        本页面不会展示 Secret、环境变量名称、原始 YAML、完整 revision 或配置文件路径，也不会自动重启任何组件。
      </aside>

      {controller.saveResult === null ? null : (
        <ConfigSaveResult
          result={controller.saveResult}
          synchronizing={controller.synchronization === "refreshing"}
          synchronizationFailed={controller.synchronization === "failed"}
          onReload={controller.refreshManually}
        />
      )}

      {controller.conflict === null ? null : (
        <ConfigConflictPanel
          kind={controller.conflict}
          onLoadLatest={controller.loadLatestAfterConflict}
        />
      )}

      {controller.loadError === null ? null : (
        <div className="page-error" role="alert">
          {controller.loadError}
        </div>
      )}
      {controller.saveError === null ? null : (
        <div className="page-error" role="alert">
          {controller.saveError}
        </div>
      )}

      {controller.phase === "loading" ? (
        <div className="config-page-state" role="status">
          <span className="loading-pulse" aria-hidden="true" />
          正在读取配置安全快照…
        </div>
      ) : null}

      {controller.phase === "error" ? (
        <div className="config-page-state config-page-error">
          <span>配置页面当前不可用。</span>
          <button
            type="button"
            className="secondary-button"
            onClick={controller.retryInitialLoad}
          >
            重新读取
          </button>
        </div>
      ) : null}

      {controller.phase === "ready" && controller.snapshot !== null ? (
        <>
          {controller.snapshot.groups.map((group) => (
            <ConfigGroup
              key={group.id}
              group={group}
              draft={controller.draft}
              errors={controller.analysis.errors}
              onDraftChange={controller.changeDraft}
            />
          ))}
          <ConfigSaveBar
            saveButtonRef={saveButtonRef}
            changeCount={controller.analysis.changes.length}
            hasErrors={controller.analysis.errors.size > 0}
            busy={busy}
            synchronizationRequired={controller.synchronization !== "idle"}
            onDiscard={controller.discardDraft}
            onSave={controller.openConfirmation}
          />
        </>
      ) : null}

      {controller.confirmationOpen ? (
        <ConfigSaveConfirmation
          changes={controller.analysis.changes}
          onCancel={controller.closeConfirmation}
          onContinue={controller.continueToControlToken}
        />
      ) : null}

      {controller.controlDialogOpen ? (
        <ControlTokenDialog
          triggerRef={saveButtonRef}
          onSubmit={controller.submitControlToken}
          onClose={controller.closeControlDialog}
        />
      ) : null}
    </>
  );
}
