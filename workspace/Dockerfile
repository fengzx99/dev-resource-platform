FROM ubuntu_24.04_template:v1
USER root
COPY entrypoint.sh /opt/drp/entrypoint.sh
RUN chmod 755 /opt/drp/entrypoint.sh && test -x /usr/sbin/sshd && usermod -d /root -s /bin/bash -p '*' root && rm -f /etc/ssh/ssh_host_*key*
LABEL io.devresources.workspace-image="1" io.devresources.read-only-root="1" io.devresources.persistent-home="1" io.devresources.home-path="/root" io.devresources.login-user="root"
EXPOSE 22
ENTRYPOINT ["/opt/drp/entrypoint.sh"]
