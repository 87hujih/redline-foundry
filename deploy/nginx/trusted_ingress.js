import crypto from 'crypto';

const identitySecretName = 'AGENT_RUNTIME_TRUSTED_INGRESS_HMAC_SECRET';

function issuedAt() {
    return new Date().toISOString();
}

function signature(r) {
    const secret = process.env[identitySecretName] || '';
    if (Buffer.byteLength(secret, 'utf8') < 32) {
        throw new Error('trusted ingress HMAC secret is unavailable');
    }
    const fields = [
        'v1',
        r.variables.trusted_request_id,
        r.method.toUpperCase(),
        r.uri,
        r.variables.auth_principal_type,
        r.variables.auth_principal_id,
        r.variables.auth_organization_id,
        r.variables.auth_workspace_id,
        r.variables.identity_issued_at,
        r.variables.auth_roles,
    ];
    if (fields.some((value) => !value)) {
        throw new Error('trusted ingress identity is incomplete');
    }
    return crypto.createHmac('sha256', secret).update(fields.join('\n')).digest('hex');
}

export default { issuedAt, signature };
