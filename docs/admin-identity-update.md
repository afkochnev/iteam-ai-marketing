# Administrator email migration

The existing administrator UUID, password hash and role are retained. Use Profile → Change login email, enter `afkochnev@gmail.com` and confirm the current application password. Do not register a second account or share the password in chat.

The authenticated endpoint serializes changes, validates the current password, normalizes the address, rejects collisions, increments auth_version, issues a replacement session cookie and records USER_EMAIL_CHANGED. Older sessions are revoked. Bootstrap refuses to provision another admin while any admin identity exists.

After accepted release deployment and successful application change, verify login using the new email and the existing password. Update the private bootstrap/operator ADMIN_EMAIL setting to the new address without printing .env or password values. Changing ADMIN_EMAIL alone does not migrate the existing database user. No manual production SQL is needed.

No production identity is changed by this PR.
