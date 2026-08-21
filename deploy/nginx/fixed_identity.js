import crypto from 'crypto';

const secretName = 'AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET';

function requiredEnvironment(name) {
    const value = (process.env[name] || '').trim();
    if (!value) {
        throw new Error(`fixed identity environment is incomplete: ${name}`);
    }
    return value;
}

function principalType() {
    const value = requiredEnvironment('DOCREVIEW_FIXED_PRINCIPAL_TYPE').toLowerCase();
    if (value !== 'user' && value !== 'service') {
        throw new Error('fixed identity principal type must be user or service');
    }
    return value;
}

function principalId() {
    return requiredEnvironment('DOCREVIEW_FIXED_PRINCIPAL_ID');
}

function organizationId() {
    return requiredEnvironment('DOCREVIEW_FIXED_ORGANIZATION_ID');
}

function workspaceId() {
    return requiredEnvironment('DOCREVIEW_FIXED_WORKSPACE_ID');
}

function roles() {
    return requiredEnvironment('DOCREVIEW_FIXED_ROLES');
}

function issuedAt() {
    return new Date().toISOString();
}

function signature(r) {
    const secret = process.env[secretName] || '';
    if (Buffer.byteLength(secret, 'utf8') < 32) {
        throw new Error('trusted ingress HMAC secret is unavailable');
    }
    const fields = [
        'v1',
        r.variables.trusted_request_id,
        r.method.toUpperCase(),
        r.uri,
        r.variables.identity_principal_type,
        r.variables.identity_principal_id,
        r.variables.identity_organization_id,
        r.variables.identity_workspace_id,
        r.variables.identity_issued_at,
        r.variables.identity_roles,
    ];
    if (fields.some((value) => !value)) {
        throw new Error('fixed trusted ingress identity is incomplete');
    }
    return crypto.createHmac('sha256', secret).update(fields.join('\n')).digest('hex');
}

export default {
    issuedAt,
    organizationId,
    principalId,
    principalType,
    roles,
    signature,
    workspaceId,
};
