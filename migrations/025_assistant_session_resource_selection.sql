ALTER TABLE assistant_sessions
    ADD COLUMN selected_resource_id uuid,
    ADD COLUMN resource_selected_at timestamp with time zone;

ALTER TABLE resources
    ADD CONSTRAINT resources_workspace_id_id_key UNIQUE (workspace_id, id);

ALTER TABLE assistant_sessions
    ADD CONSTRAINT assistant_sessions_selected_resource_workspace_fk
        FOREIGN KEY (workspace_id, selected_resource_id)
        REFERENCES resources (workspace_id, id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    ADD CONSTRAINT assistant_sessions_resource_selection_pair_check
        CHECK (
            (selected_resource_id IS NULL AND resource_selected_at IS NULL)
            OR
            (selected_resource_id IS NOT NULL AND resource_selected_at IS NOT NULL)
        );
