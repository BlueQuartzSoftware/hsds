Installation with Azure Kubernetes
==================================

**Note:** These instructions assume you are using a Linux based system. If you are using Windows please see the special notes at the end.

To begin, export environment variables as shown in "Sample .bashrc" below.

These environment variables will be used to create Azure resources.

    export RESOURCEGROUP=myresourcegroup
    export AKSCLUSTER=myakscluster
    export LOCATION=westus
    export ACRNAME=myacrname
    export STORAGEACCTNAME=mystorageaccount
    export CONTAINERNAME=testcontainer
    
    # the following will be the same as the variables exported on the cluster below
    # use the connection string for your Azure account. Note the quotation marks around the string
    export AZURE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=myacct;AccountKey=GZJxxxOPnw==;EndpointSuffix=core.windows.net"
    # set to the name of the container you will be using export STORAGEACCTNAME=home

Prerequisites
-------------

Setup Pip and Python 3 on your local machine if not already installed (e.g. with Miniconda <https://docs.conda.io/en/latest/miniconda.html>)

Setup your Azure environment
----------------------------

Here we will deploy an Azure Storage Account, Azure Container Registry (ACR) and Azure Kubernetes Service (AKS).

1. Install az-cli: `pip install azure-cli`
2. Validate runtime version az-cli is at least 2.0.80: `az version`
3. Log in to Azure Subscription using AZ-Cli. `$az login`
4. After successful login, the list of available subscriptions will be displayed. If you have access to more than one subscription, set the proper subscription to be used: `az account set --subscription [name]`
5. Run the following commands to create Azure Resource Group: `az group create --name $RESOURCEGROUP --location $LOCATION`
6. Create storage account: `az storage account create -n $STORAGEACCTNAME -g $RESOURCEGROUP -l $LOCATION --sku Standard_LRS`
7. Create a blob container in the storage account: `$az storage container create -n $CONTAINERNAME --account-name $STORAGEACCTNAME --fail-on-exist`
Note: The connection string for the storage account can be found in the portal under Settings > Access keys on the storage account or via this cli command: `az storage account show-connection-string -n $STORAGEACCTNAME -g $RESOURCEGROUP`
8. The following command will create the new ACR: `az acr create --resource-group $RESOURCEGROUP --name $ACRNAME --sku Basic --admin-enabled true`
9. Install AKS cli: `az aks install-cli`
10. Create AKS Cluster and attach to ACR: `az aks create -n $AKSCLUSTER -g $RESOURCEGROUP --generate-ssh-keys --attach-acr $ACRNAME`
11. Get access to the AKS Cluster: `az aks get-credentials -g $RESOURCEGROUP -n $AKSCLUSTER`

Prepare and deploy your docker image to ACR
-------------------------------------------

Currently the HSDS server uses simple auth and the login credentials are embedded in the code. The following procedure builds a docker image with your custom set of credentials. If you are just deploying the HSDS server **for testing purposes only**, import the HDF group's docker image as is into ACR as follows and skip the rest of this section: `az acr import -n $ACRNAME --source docker.io/hdfgroup/hsds:latest --image hsds:v1`

1. Clone the hsds repository in a local folder: `git clone https://github.com/HDFGroup/hsds`
2. Go to admin/config directory: `cd hsds/admin/config`
3. Copy the file "passwd.default" to "passwd.txt".
4. Add/change usernames/passwords that you want to use. **Note**: Do not keep the original example credentials.
5. From hsds directory, build docker image: `bash build.sh`
6. Tag the docker image using the ACR scheme: `docker tag hdfgroup/hsds $ACRNAME.azurecr.io/hsds:v1` where $ACRNAME is the ACR being deployed to, and v1 is the version (update this every time you will be deploying a new version of HSDS).
7. Login to the Azure container registry (ACR): `az acr login --name $ACRNAME`
8. You may also need to login into ACR from docker as follows: Get the ACR admin credentials: `az acr credential show -n $ACRNAME` then docker login with those credentials: `docker login $ACRNAME -u xxx -p xxx`
9. Push the image to Azure ACR: `docker push $ACRNAME.azurecr.io/hsds:v1`
  **Note:** Use all lowercase ACRNAME in these commands if your actual ACRNAME includes uppercase characters

Deploy HSDS to AKS
------------------

1. Set the Azure Connection String as Kubernetes secret to pass to the containers by running ***k8s_make_secrets_azure.sh***
2. Create RBAC roles: `kubectl create -f k8s_rbac.yml`
3. Create HSDS deployment on the AKS cluster: `$ kubectl apply -f k8s_service_lb_azure.yml`
4. This will create an external load balancer with an http endpoint with a public-ip. 
   Use kubectl to get the public-ip of the hsds service: `$kubectl get service` 
   You should see an entry similar to:

       NAME    TYPE           CLUSTER-IP     EXTERNAL-IP      PORT(S)        AGE
       hsds    LoadBalancer   10.0.242.109   20.36.17.252     80:30326/TCP   23

   Note the public-ip (EXTERNAL-IP). This is where you can access the HSDS service externally. It may take some time for the EXTERNAL-IP to show up after the service deployment.  For additional configuration options to handle SSL related scenarios please see: *frontdoor_install_azure.md*
   Additional reference for Azure Front Door <https://docs.microsoft.com/en-us/azure/frontdoor/>
5. Now we will deploy the HSDS containers. In ***k8s_deployment_azure.yml***, customize the values for:
   env sections:
    * HSDS_ENDPOINT (change to `http://public-ip` where pubic-ip is the EXTERNAL-IP from step 3 above)
    * BUCKET_NAME (this is the name of the blob container created earlier)
   containers sections
    * image: 'myacrname.azurecr.io/hsds:v1' to reflect the acr repository for deployment.
6. Apply the deployment: `$ kubectl apply -f k8s_deployment_azure.yml`
7. Verify that the HSDS pod is running: `$ kubectl get pods`  a pod with a name starting with hsds should be displayed with status as "Running".
8. Additional verification: Run (`$ kubectl describe pod hsds-xxxx`) and make sure everything looks OK
9. To locally test that HSDS functioning
    * Create a forwarding port to the Kubernetes service `$ sudo kubectl port-forward hsds-1234 8080:5101` (use another port if 8080 is unavailable)
    * From a browser hit: <http://127.0.0.1:8080/about> and verify that "cluster_state" is "READY"

Test the Deployment using Integration Test and Test Data
--------------------------------------------------------

1. Install h5pyd: `pip install h5pyd`
2. Run: `hsconfigure` and set:
    * hs endpoint: e.g. <http://EXTERNAL-IP>)
    * admin username/password (added to passwd.txt earlier)
    * Ignore API Key
3. Run: `hsinfo`.  Server state should be "`READY`".  Ignore the "Not Found" error for the admin home folder
4. Create "/home" folder: `$ hstouch /home/`.  Note: trailing slash is important!
5. For each username in the passwd file, create a top-level domain: `hstouch -u <username> -p <passwd> /home/<username>/test/`
6. Run the integration test: `python testall.py --skip_unit`
7. Download the following file: `wget https://s3.amazonaws.com/hdfgroup/data/hdf5test/tall.h5`
8. Import into hsds: `hsload -v -u <username> -p <passwd> tall.h5 /home/<username>/test/`
9. Verify upload: `hsls -r -u test_user1 -p <passwd> /home/test_user1/test/tall.h5`

AKS Cluster Scaling
-------------------

To scale up or down the number of HSDS pods, run:
`$kubectl scale --replicas=n deployment/hsds` where n is the number of pods desired.

Notes for Installation from a Windows Machine
---------------------------------------------

Follow the instructions above with the following modifications in the respective sections

1. Before you start make sure that you have docker installed on your system by running: `docker --version` otherwise install docker desktop: <https://docs.docker.com/docker-for-windows/>
2. Sample .bashrc will not work on Windows - instead run the following commands on the console (or include them in a batch file and run the batch file)

       SET AZURE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=myacct;AccountKey=GZJxxxOPnw==;EndpointSuffix=core.windows.net"
       SET BUCKET_NAME=home
       SET RESOURCEGROUP=myresourcegroup
       SET AKSCLUSTER=myakscluster
       SET LOCATION=westus
       SET ACRNAME=myacrname
       SET STORAGEACCTNAME=mystorageaccount
       SET CONTAINERNAME=testcontainer

   For commands in all sections replace the unix environment variable notation (SVAR) with Windows notation (%VAR%).  For example instead of `$ACRNAME` use `%ACRNAME%`
3. Setup your Azure environment, to install Azure cli on Windows, follow instructions here: <https://docs.microsoft.com/en-us/cli/azure/install-azure-cli-windows?view=azure-cli-latest>
4. Prepare and deploy your docker image to ACR
   To create kubernetes secret:

    * Enter the Azure connection string (just the string, not the set command) in a file named ***az_conn_str*** without double quotes (") or the end-of-line.
    * Run `kubectl create secret generic azure-conn-str --from-file=` ***az_conn_str***
    * Delete ***az_conn_str***

   On Windows downloaded files have CRLF instead of LF. This will cause the container to fail. To solve this:

    * Download do2unix from: <https://sourceforge.net/projects/dos2unix/>
    * Apply dos2unix to entrypoint.sh: `dos2unix entrypoint.sh`
    * build.sh will not run on Windows, instead run the docker build directly: `docker build -t ACRNAME.azurecr.io/hsds:v1 .`

  **Note:** This will not run the pyflakes on the code. Pyflakes is a code checker and not essential to building the container.
